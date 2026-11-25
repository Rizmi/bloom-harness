if not __package__:
    import os
    import sys

    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from bloom.agent import cli
else:
    from .agent import cli

if __name__ == "__main__":
    cli()
