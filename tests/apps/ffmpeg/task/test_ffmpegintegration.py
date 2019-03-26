import os

from apps.transcoding.common import TranscodingTaskBuilderException, \
    ffmpegException
from apps.transcoding.ffmpeg.task import ffmpegTaskTypeInfo
from golem.testutils import TestTaskIntegration


class FfmpegIntegrationTestCase(TestTaskIntegration):

    def setUp(self):
        super(FfmpegIntegrationTestCase, self).setUp()
        self.RESOURCES = os.path.join(os.path.dirname(
            os.path.dirname(os.path.realpath(__file__))), 'resources')
        self.tt = ffmpegTaskTypeInfo()

    @classmethod
    def _create_task_def_for_transcoding(cls, resource_stream, result_file):
        return {
            'type': 'FFMPEG',
            'name': os.path.splitext(os.path.basename(result_file))[0],
            'timeout': '0:10:00',
            'subtask_timeout': '0:09:50',
            'subtasks_count': 2,
            'bid': 1.0,
            'resources': [resource_stream],
            'options': {
                'output_path': os.path.dirname(result_file),
                'video': {
                    'codec': 'h265',
                    'resolution': [320, 240],
                    'frame_rate': "25"
                },
                'container': os.path.splitext(result_file)[1][1:]
            }
        }


class TestffmpegIntegration(FfmpegIntegrationTestCase):

    def test_simple_case(self):
        resource_stream = os.path.join(self.RESOURCES, 'test_video2.mp4')
        result_file = os.path.join(self.root_dir, 'test_simple_case.mp4')
        task_def = self._create_task_def_for_transcoding(
            resource_stream,
            result_file)

        self.execute_task(task_def)

        self.run_asserts([
            self.check_file_existence(result_file)])

    def test_nonexistent_output_dir(self):
        resource_stream = os.path.join(self.RESOURCES, 'test_video2.mp4')
        result_file = os.path.join(self.root_dir, 'nonexistent', 'path',
                                   'test_invalid_task_definition.mp4')
        task_def = self._create_task_def_for_transcoding(
            resource_stream,
            result_file)

        self.execute_task(task_def)

        self.run_asserts([
            self.check_dir_existence(os.path.dirname(result_file)),
            self.check_file_existence(result_file)])

    def test_nonexistent_resource(self):
        resource_stream = os.path.join(self.RESOURCES,
                                       'test_nonexistent_video.mp4')

        result_file = os.path.join(self.root_dir, 'test_nonexistent_video.mp4')
        task_def = self._create_task_def_for_transcoding(
            resource_stream,
            result_file)

        with self.assertRaises(TranscodingTaskBuilderException):
            self.execute_task(task_def)

    def test_invalid_resource_stream(self):
        resource_stream = os.path.join(self.RESOURCES, 'invalid_test_video.mp4')
        result_file = os.path.join(self.root_dir,
                                   'test_invalid_resource_stream.mp4')

        task_def = self._create_task_def_for_transcoding(
            resource_stream,
            result_file)

        with self.assertRaises(ffmpegException):
            self.execute_task(task_def)

    def test_task_invalid_params(self):
        resource_stream = os.path.join(self.RESOURCES, 'test_video2.mp4')
        result_file = os.path.join(self.root_dir, 'test_invalid_params.mp4')
        task_def = self._create_task_def_for_transcoding(
            resource_stream,
            result_file)
        task_def['options']['video']['codec'] = 'abcd'

        with self.assertRaises(TranscodingTaskBuilderException):
            self.execute_task(task_def)
