import golem_messages
import semantic_version

import golem
from golem import utils

# Semantic version instances
# -----------------------------------------------------------------------------
# If you want to use string versions, please use originals:
# golem.__version__ for version of golem core
# golem_messages.__version__ for version of golem-messages library

GOLEM_VERSION = semantic_version.Version(golem.__version__)
# Oldest version that is backwards compatible with us
GOLEM_MIN_VERSION = utils.get_min_version(GOLEM_VERSION)
GOLEM_SPEC = utils.get_version_spec(GOLEM_VERSION)
GOLEM_MESSAGES_VERSION = semantic_version.Version(golem_messages.__version__)
GOLEM_MESSAGES_SPEC = utils.get_version_spec(GOLEM_MESSAGES_VERSION)
