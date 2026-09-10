import sys

from .cli import main

if __name__ == "__main__":
    # sys.exit, not a bare call: `notify` and `ebay check` return 78
    # (EX_CONFIG) to say a configuration error is permanent, and the
    # systemd units carry RestartPreventExitStatus=78 so a permanent
    # error stays dead where it can be seen. Discarding the return value
    # turned every one of those refusals into a success - invisible via
    # the installed `mplabel` script, which setuptools wraps in
    # sys.exit(), and wrong for `python -m mplabel`, which is what the
    # documentation and every test run uses.
    sys.exit(main())
