# pylint: disable=protected-access
import random
import time
import uuid
import unittest
import unittest.mock as mock
from os import urandom

import golem_sci
from golem_sci.interface import TransactionReceipt
from eth_utils import encode_hex
from ethereum.utils import denoms, privtoaddr
from freezegun import freeze_time
from hexbytes import HexBytes

from golem.core import variables
from golem.core.common import timestamp_to_datetime
from golem.ethereum.paymentprocessor import (
    PaymentProcessor,
    PAYMENT_MAX_DELAY,
)
from golem.model import Payment, PaymentStatus, PaymentDetails
from golem.testutils import DatabaseFixture

from tests.factories import model as model_factory


class PaymentStatusTest(unittest.TestCase):
    def test_status(self):
        s = PaymentStatus(1)
        self.assertEqual(s, PaymentStatus.awaiting)


class PaymentProcessorBase(DatabaseFixture):
    def setUp(self):
        DatabaseFixture.setUp(self)
        self.addr = encode_hex(privtoaddr(urandom(32)))
        self.sci = mock.Mock(spec=golem_sci.SmartContractsInterface)
        self.sci.GAS_PRICE = 20
        self.sci.GAS_PER_PAYMENT = 300
        self.sci.GAS_BATCH_PAYMENT_BASE = 30
        self.sci.get_eth_balance.return_value = 0
        self.sci.get_gntb_balance.return_value = 0
        self.sci.get_eth_address.return_value = self.addr
        self.sci.get_current_gas_price.return_value = self.sci.GAS_PRICE
        self.sci.get_gate_address.return_value = None
        latest_block = mock.Mock(golem_sci.Block)
        latest_block.gas_limit = 10 ** 10
        self.sci.get_latest_confirmed_block.return_value = latest_block
        self.tx_hash = '0xdead'
        self.sci.batch_transfer.return_value = self.tx_hash

        self.pp = PaymentProcessor(self.sci)
        self.pp._gnt_converter = mock.Mock()
        self.pp._gnt_converter.is_converting.return_value = False
        self.pp._gnt_converter.get_gate_balance.return_value = 0


class PaymentProcessorInternalTest(PaymentProcessorBase):
    """ In this suite we test internal logic of PaymentProcessor. The final
        Ethereum transactions are not inspected.
    """
    def test_load_from_db_awaiting(self):
        self.assertEqual([], self.pp._awaiting)

        value = 10
        payment = Payment.create(
            subtask=str(uuid.uuid4()),
            payee=urandom(20),
            value=value,
        )

        self.pp.load_from_db()
        expected = [payment]
        self.assertEqual(expected, self.pp._awaiting)
        self.assertEqual(value, self.pp.reserved_gntb)
        self.assertLess(0, self.pp.recipients_count)

    def test_load_from_db_sent(self):
        tx_hash1 = encode_hex(urandom(32))
        tx_hash2 = encode_hex(urandom(32))
        value = 10
        payee = urandom(20)
        sent_payment11 = Payment.create(
            subtask=str(uuid.uuid4()),
            payee=payee,
            value=value,
            details=PaymentDetails(tx=tx_hash1[2:]),
            status=PaymentStatus.sent
        )
        sent_payment12 = Payment.create(
            subtask=str(uuid.uuid4()),
            payee=payee,
            value=value,
            details=PaymentDetails(tx=tx_hash1[2:]),
            status=PaymentStatus.sent
        )
        sent_payment21 = Payment.create(
            subtask=str(uuid.uuid4()),
            payee=payee,
            value=value,
            details=PaymentDetails(tx=tx_hash2[2:]),
            status=PaymentStatus.sent
        )
        self.pp.load_from_db()
        self.assertEqual(3 * value, self.pp.reserved_gntb)
        self.assertEqual(0, self.pp.recipients_count)
        assert self.sci.on_transaction_confirmed.call_count == 2
        assert self.sci.on_transaction_confirmed.call_args_list[0][0][0] == \
            tx_hash1
        assert self.sci.on_transaction_confirmed.call_args_list[1][0][0] == \
            tx_hash2
        with mock.patch('golem.ethereum.paymentprocessor.threads') as threads:
            self.sci.on_transaction_confirmed.call_args_list[0][0][1](
                mock.Mock())
            threads.deferToThread.assert_called_once_with(
                self.pp._on_batch_confirmed,
                [sent_payment11, sent_payment12],
                mock.ANY,
            )
            threads.reset_mock()
            self.sci.on_transaction_confirmed.call_args_list[1][0][1](
                mock.Mock())
            threads.deferToThread.assert_called_once_with(
                self.pp._on_batch_confirmed,
                [sent_payment21],
                mock.ANY,
            )

    def test_recipients_count(self):
        assert self.pp.recipients_count == 0

    def test_monitor_progress(self):
        balance_eth = 1 * denoms.ether
        balance_gntb = 99 * denoms.ether
        gas_price = 10 ** 9
        self.sci.get_eth_balance.return_value = balance_eth
        self.sci.get_gntb_balance.return_value = balance_gntb
        self.sci.get_transaction_gas_price.return_value = gas_price
        self.pp.CLOSURE_TIME_DELAY = 0

        assert self.pp.reserved_gntb == 0
        assert self.pp.recipients_count == 0

        gnt_value = 10**17
        self.pp.add("test_subtask_id", encode_hex(urandom(20)), gnt_value)
        assert self.pp.reserved_gntb == gnt_value
        assert self.pp.recipients_count == 1

        tx_hash = '0xdead'
        self.sci.batch_transfer.return_value = tx_hash
        assert self.pp.sendout(0)
        assert self.sci.batch_transfer.call_count == 1
        self.sci.on_transaction_confirmed.assert_called_once_with(
            tx_hash,
            mock.ANY,
        )

        tx_block_number = 1337
        tx_timestamp = 1541766000.5
        self.sci.get_latest_confirmed_block_number.return_value = \
            tx_block_number
        self.sci.get_block_by_number.return_value = mock.Mock(
            timestamp=tx_timestamp)
        receipt = TransactionReceipt({
            'transactionHash': HexBytes(tx_hash),
            'blockNumber': tx_block_number,
            'blockHash': HexBytes('0x' + 64 * 'f'),
            'gasUsed': 55001,
            'status': 1,
        })
        with mock.patch('golem.ethereum.paymentprocessor.threads') as threads:
            self.sci.on_transaction_confirmed.call_args[0][1](receipt)
            threads.deferToThread.call_args[0][0](
                *threads.deferToThread.call_args[0][1:])

        p = Payment.get()
        self.assertEqual(p.status, PaymentStatus.confirmed)
        self.assertEqual(p.details.block_number, tx_block_number)
        self.assertEqual(p.details.block_hash, 64 * 'f')
        self.assertEqual(p.details.fee, 55001 * gas_price)
        self.assertEqual(self.pp.reserved_gntb, 0)

    def test_failed_transaction(self):
        balance_eth = 1 * denoms.ether
        balance_gntb = 99 * denoms.ether
        self.sci.get_eth_balance.return_value = balance_eth
        self.sci.get_gntb_balance.return_value = balance_gntb

        gnt_value = 10**17
        self.pp.add("test_subtask_id", encode_hex(urandom(20)), gnt_value)

        self.pp.CLOSURE_TIME_DELAY = 0
        tx_hash = '0xdead'
        self.sci.batch_transfer.return_value = tx_hash
        assert self.pp.sendout(0)

        tx_block_number = 1337
        receipt = TransactionReceipt({
            'transactionHash': HexBytes(tx_hash),
            'blockNumber': tx_block_number,
            'blockHash': HexBytes('0x' + 64 * 'f'),
            'gasUsed': 55001,
            'status': 0,
        })
        with mock.patch('golem.ethereum.paymentprocessor.threads') as threads:
            self.sci.on_transaction_confirmed.call_args[0][1](receipt)
            threads.deferToThread.call_args[0][0](
                *threads.deferToThread.call_args[0][1:])
        assert self.pp.reserved_gntb == gnt_value
        assert len(self.pp._awaiting) == 1

    def test_payment_timestamp(self):
        self.sci.get_eth_balance.return_value = denoms.ether

        ts = 7000000
        with freeze_time(timestamp_to_datetime(ts)):
            processed_ts = self.pp.add(
                "test_subtask_id",
                encode_hex(urandom(20)),
                1,
            )
        self.assertEqual(ts, processed_ts)


def _add_payment(pp, value=None, ts=None):
    payee = encode_hex(urandom(20))
    value = value if value else random.randint(1, 10)
    if not ts:
        ts = int(time.time())
    with freeze_time(timestamp_to_datetime(ts)):
        pp.add(uuid.uuid4(), payee, value)
    return golem_sci.Payment(payee, value)


class InteractionWithSmartContractInterfaceTest(PaymentProcessorBase):
    def _assert_batch_transfer_called_with(
            self,
            payments,
            closure_time: int) -> None:
        self.sci.batch_transfer.assert_called_with(mock.ANY, closure_time)
        called_payments = self.sci.batch_transfer.call_args[0][0]
        assert len(called_payments) == len(payments)
        for expected, actual in zip(payments, called_payments):
            assert expected.payee == actual.payee
            assert expected.amount == actual.amount

    def test_batch_transfer(self):
        deadline = PAYMENT_MAX_DELAY
        self.pp.CLOSURE_TIME_DELAY = 0
        self.sci.get_eth_balance.return_value = denoms.ether
        self.sci.get_gnt_balance.return_value = 0
        self.sci.get_gntb_balance.return_value = 1000 * denoms.ether

        assert not self.pp.sendout()
        self.sci.batch_transfer.assert_not_called()

        ts1 = 1230000
        ts2 = ts1 + 2 * deadline
        scip1 = _add_payment(self.pp, ts=ts1)
        scip2 = _add_payment(self.pp, ts=ts2)

        with freeze_time(timestamp_to_datetime(ts1 + deadline - 1)):
            assert not self.pp.sendout()
            self.sci.batch_transfer.assert_not_called()
        with freeze_time(timestamp_to_datetime(ts1 + deadline + 1)):
            assert self.pp.sendout()
            self._assert_batch_transfer_called_with(
                [scip1],
                ts1,
            )
            self.sci.batch_transfer.reset_mock()

        with freeze_time(timestamp_to_datetime(ts2 + deadline - 1)):
            assert not self.pp.sendout()
            self.sci.batch_transfer.assert_not_called()
        with freeze_time(timestamp_to_datetime(ts2 + deadline + 1)):
            assert self.pp.sendout()
            self._assert_batch_transfer_called_with(
                [scip2],
                ts2,
            )
            self.sci.batch_transfer.reset_mock()

    def test_closure_time(self):
        self.sci.get_eth_balance.return_value = denoms.ether
        self.sci.get_gnt_balance.return_value = 0
        self.sci.get_gntb_balance.return_value = 1000 * denoms.ether

        scip1 = _add_payment(self.pp, ts=1000000)
        scip2 = _add_payment(self.pp, ts=2000000)
        scip5 = _add_payment(self.pp, ts=5000000)

        closure_time = 2000000
        time_value = closure_time + self.pp.CLOSURE_TIME_DELAY
        with freeze_time(timestamp_to_datetime(time_value)):
            self.pp.sendout(0)
            self._assert_batch_transfer_called_with(
                [scip1, scip2],
                closure_time)
            self.sci.batch_transfer.reset_mock()

        closure_time = 4000000
        time_value = closure_time + self.pp.CLOSURE_TIME_DELAY
        with freeze_time(timestamp_to_datetime(time_value)):
            self.pp.sendout(0)
            self.sci.batch_transfer.assert_not_called()
            self.sci.batch_transfer.reset_mock()

        closure_time = 5000000
        time_value = closure_time + self.pp.CLOSURE_TIME_DELAY
        with freeze_time(timestamp_to_datetime(time_value)):
            self.pp.sendout(0)
            self._assert_batch_transfer_called_with(
                [scip5],
                closure_time)
            self.sci.batch_transfer.reset_mock()

    def test_short_on_gnt(self):
        self.sci.get_eth_balance.return_value = denoms.ether
        self.sci.get_gnt_balance.return_value = 0
        self.sci.get_gntb_balance.return_value = 4 * denoms.ether
        self.pp.CLOSURE_TIME_DELAY = 0

        scip1 = _add_payment(self.pp, value=1 * denoms.ether, ts=1)
        scip2 = _add_payment(self.pp, value=2 * denoms.ether, ts=2)
        scip5 = _add_payment(self.pp, value=5 * denoms.ether, ts=3)

        with freeze_time(timestamp_to_datetime(10000)):
            self.pp.sendout(0)
            self._assert_batch_transfer_called_with(
                [scip1, scip2],
                2)
            self.sci.batch_transfer.reset_mock()

        self.sci.get_gntb_balance.return_value = 5 * denoms.ether
        with freeze_time(timestamp_to_datetime(10000)):
            self.pp.sendout(0)
            self._assert_batch_transfer_called_with(
                [scip5],
                3)
            self.sci.batch_transfer.reset_mock()

    def test_short_on_gnt_closure_time(self):
        self.sci.get_eth_balance.return_value = denoms.ether
        self.sci.get_gnt_balance.return_value = 0
        self.sci.get_gntb_balance.return_value = 4 * denoms.ether
        self.pp.CLOSURE_TIME_DELAY = 0
        ts1 = 1000
        ts2 = 2000

        scip1 = _add_payment(self.pp, value=1 * denoms.ether, ts=ts1)
        scip2 = _add_payment(self.pp, value=2 * denoms.ether, ts=ts2)
        scip5 = _add_payment(self.pp, value=5 * denoms.ether, ts=ts2)

        with freeze_time(timestamp_to_datetime(10000)):
            self.pp.sendout(0)
            self._assert_batch_transfer_called_with(
                [scip1],
                ts1)
            self.sci.batch_transfer.reset_mock()

        self.sci.get_gntb_balance.return_value = 10 * denoms.ether
        with freeze_time(timestamp_to_datetime(10000)):
            self.pp.sendout(0)
            self._assert_batch_transfer_called_with(
                [scip2, scip5],
                ts2)
            self.sci.batch_transfer.reset_mock()

    def test_short_on_eth(self):
        self.sci.get_eth_balance.return_value = self.sci.GAS_PRICE * \
            (self.sci.GAS_BATCH_PAYMENT_BASE + 2 * self.sci.GAS_PER_PAYMENT)
        self.sci.get_gnt_balance.return_value = 0
        self.sci.get_gntb_balance.return_value = 1000 * denoms.ether
        self.pp.CLOSURE_TIME_DELAY = 0

        scip1 = _add_payment(self.pp, value=1, ts=1)
        scip2 = _add_payment(self.pp, value=2, ts=2)
        scip5 = _add_payment(self.pp, value=5, ts=3)

        with freeze_time(timestamp_to_datetime(10000)):
            self.pp.sendout(0)
            self._assert_batch_transfer_called_with(
                [scip1, scip2],
                2)
            self.sci.batch_transfer.reset_mock()

        self.sci.get_eth_balance.return_value = denoms.ether
        with freeze_time(timestamp_to_datetime(10000)):
            self.pp.sendout(0)
            self._assert_batch_transfer_called_with(
                [scip5],
                3)
            self.sci.batch_transfer.reset_mock()

    def test_sorted_payments(self):
        self.sci.get_eth_balance.return_value = 1000 * denoms.ether
        self.sci.get_gnt_balance.return_value = 0
        self.sci.get_gntb_balance.return_value = 1000 * denoms.ether
        self.pp.CLOSURE_TIME_DELAY = 0

        _add_payment(self.pp, value=1, ts=300000)
        scip2 = _add_payment(self.pp, value=2, ts=200000)
        scip3 = _add_payment(self.pp, value=3, ts=100000)

        with freeze_time(timestamp_to_datetime(200000)):
            self.pp.sendout(0)
            self._assert_batch_transfer_called_with([scip3, scip2], 200000)

    def test_batch_transfer_throws(self):
        self.sci.get_eth_balance.return_value = 1000 * denoms.ether
        self.sci.get_gnt_balance.return_value = 0
        self.sci.get_gntb_balance.return_value = 1000 * denoms.ether
        self.pp.CLOSURE_TIME_DELAY = 0

        ts = 100000
        scip = _add_payment(self.pp, value=1, ts=ts)
        self.sci.batch_transfer.side_effect = Exception

        with freeze_time(timestamp_to_datetime(ts)):
            with self.assertRaises(Exception):
                self.pp.sendout(0)
            self._assert_batch_transfer_called_with([scip], ts)
            self.sci.batch_transfer.reset_mock()

        self.sci.batch_transfer.side_effect = None
        with freeze_time(timestamp_to_datetime(ts)):
            self.pp.sendout(0)
            self._assert_batch_transfer_called_with([scip], ts)

    def test_block_gas_limit(self):
        self.sci.get_eth_balance.return_value = denoms.ether
        self.sci.get_gnt_balance.return_value = 0
        self.sci.get_gntb_balance.return_value = 1000 * denoms.ether
        self.sci.get_latest_confirmed_block.return_value.gas_limit = \
            (self.sci.GAS_BATCH_PAYMENT_BASE + self.sci.GAS_PER_PAYMENT) /\
            self.pp.BLOCK_GAS_LIMIT_RATIO
        self.pp.CLOSURE_TIME_DELAY = 0

        scip1 = _add_payment(self.pp, value=1, ts=1)
        _add_payment(self.pp, value=2, ts=2)

        with freeze_time(timestamp_to_datetime(10000)):
            self.pp.sendout(0)
            self._assert_batch_transfer_called_with(
                [scip1],
                1)
            self.sci.batch_transfer.reset_mock()


class UpdateOverdueTest(PaymentProcessorBase):
    def add_payment(self, processed_ts: int):
        payment = model_factory.Payment(processed_ts=processed_ts)
        payment.save(force_insert=True)
        self.pp._awaiting.add(payment)
        return payment

    def add_current_payment(self):
        return self.add_payment(int(time.time()))

    def add_overdue_payment(self):
        deadline = int(time.time()) - variables.PAYMENT_DEADLINE
        return self.add_payment(deadline - random.randint(1, 100))

    def test_no_overdues(self):
        payment = self.add_current_payment()
        self.pp.update_overdue()
        self.assertIs(payment.refresh().status, PaymentStatus.awaiting)

    def test_one_overdue(self):
        payment = self.add_current_payment()
        payment_overdue = self.add_overdue_payment()
        self.pp.update_overdue()
        self.assertIs(payment.refresh().status, PaymentStatus.awaiting)
        self.assertIs(payment_overdue.refresh().status, PaymentStatus.overdue)

    def test_all_overdues(self):
        payments = [self.add_overdue_payment() for _ in range(10)]
        self.pp.update_overdue()
        for payment_overdue in payments:
            self.assertIs(
                payment_overdue.refresh().status,
                PaymentStatus.overdue,
            )

    def test_already_overdue(self):
        payment_overdue = self.add_overdue_payment()
        payment_overdue.status = PaymentStatus.overdue
        payment_overdue.save()
        self.pp.update_overdue()
        self.assertIs(payment_overdue.refresh().status, PaymentStatus.overdue)
