"""Fixed LabMCP worker entry, shared by source and portable distributions."""
import sys

from .runtime import protect_worker_parent


def main(arguments=None):
    protect_worker_parent()
    sys.argv = ["labmcp_epics.server", *(sys.argv[1:] if arguments is None else arguments)]
    from labmcp_epics.server import main as serve
    serve()


if __name__ == "__main__":
    main()
