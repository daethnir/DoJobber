#!/usr/bin/env python3

from dojobber import DummyJob
from dojobber import RunonlyJob


class Passful(DummyJob):
    pass


class BrokenInit(RunonlyJob):
    DEPS = [Passful]

    def __init__(self):
        raise RuntimeError('die die die')

    def Check(self, *dummy_args, **dummy_kwargs):
        return True


class Top00(DummyJob):
    DEPS = [BrokenInit]
    pass


if __name__ == '__main__':
    import sys
    sys.stderr.write('This is a library only.\n')
