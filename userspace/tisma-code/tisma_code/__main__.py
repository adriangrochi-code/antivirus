import sys

if sys.argv[1:2] == ["motor"]:
    from .motor import cli
    sys.exit(cli(sys.argv[2:]))

from .server import main

sys.exit(main())
