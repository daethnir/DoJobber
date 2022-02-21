#!/usr/bin/env python3
"""More Tests module"""

from dojobber import DummyJob
from dojobber import RunonlyJob

# pylint:disable=missing-docstring
# pylint:disable=too-few-public-methods


class Passful(DummyJob):
    pass


class BrokenInit(RunonlyJob):
    DEPS = [Passful]

    def __init__(self):  # pylint: disable=super-init-not-called
        raise RuntimeError('die die die')

    def Check(self, *_, **__):
        return True


class Top00(DummyJob):
    DEPS = [BrokenInit]


if __name__ == '__main__':
    import sys
    sys.stderr.write('This is a library only.\n')
