#!/usr/bin/env python
"""DoJobber Class."""

# standard
import logging
import os
import sys
import time
import traceback
import subprocess
import distutils.spawn

from pygraph.algorithms import cycles
from pygraph.algorithms.searching import depth_first_search
from pygraph.classes.digraph import digraph


# Determine if we have external dependencies to generate/show graphs
try:
    from pygraph.readwrite import dot
except ImportError:
    sys.stderr.write(
        '** Graphs will not be supported'
        ' (cannot import pygraph.readwrite.dot - pip install )\n'
    )
    dot = None  # pylint:disable=invalid-name
if dot and not distutils.spawn.find_executable('dot'):
    dot = None  # pylint:disable=invalid-name
    sys.stderr.write(
        '** Graphs will not be supported'
        ' (no dot executable - install graphviz)\n'
    )
DISPLAY = True
if not distutils.spawn.find_executable('display'):
    DISPLAY = False
    sys.stderr.write(
        '** display_graph will not be supported'
        ' (no display executable - install imagemagick.\n'
    )

# Disable some pylint warnings
# pylint:disable=invalid-name
# pylint:disable=too-few-public-methods


class Job:  # pylint:disable=too-many-instance-attributes
    """Job Class."""

    TRIES = None  # Override in your Job if desired
    RETRY_DELAY = None  # Override in your Job if desired

    def __init__(self):  # pylint:disable=super-init-not-called
        """Initialization.

        attributes:
            storage - the local storage, used between Check/Run executions
            global_storage - global checknrun storage

            _check_phase - either 'check' or 'recheck', depending on if this
                          the first check or the post-Run check.
                          In general you should not do anything different
                          based on check or recheck, but this could be useful
                          for mocks and other trickery.

            _check_results - the result of the Check, if succesful, else None
            _check_exception - the exception from Check, if unsuccessful,
                               else None
            _run_results - the result of the Run, if successful, else None
            _run_exception - the exception from the Run, if unsuccessful,
                             else None
            _recheck_results - the result of the re-Check, if succesful,
                               else None
            _recheck_exception - the exception from re-Check, if unsuccessful,
                                 else None
        """

        self.storage = None
        self.global_storage = None

        # These are provided for advanced Check/Run methods.
        # Using these is not actually advisable - if all your
        # work is idempontent, this is unnecessary. May break
        # at any time. YMMV. HAND. OMGWTFBBQ.
        self._check_phase = None
        self._check_results = None
        self._check_exception = None
        self._run_results = None
        self._run_exception = None
        self._recheck_results = None
        self._recheck_exception = None

    def _set_storage(self, storage, global_storage):
        """Set the storage dictionaries.

        These are set by the DoJobber.
        storage is used for storing state between checks and runs of the
        same Job. It is initialized at each check/run/check phase.

        global_storage is shared between all nodes in a DoJobber run.
        It is up to the Job authors to play nicely with each other
        in storing and retrieving data from this dictionary. Best practice
        is to create a subdictionary with your node name, e.g.

          self.global_storage['MyNodeClass']['something'] = value

        global_storage is initialized on
        and made availablet
        """
        self.storage = storage
        self.global_storage = global_storage


class RunonlyJob(Job):
    """A Job that only does the 'run' phase.

    This node will run your Run method exactly
    once. If it does not raise an exception,
    we consider that a pass.

    You *MUST NOT* include a Check method to your class.

    Even though you do not provide a Check, we run all
    Check/Run/Check phases. The initial Check always fails,
    causing your Run to execute. On success, the final
    Check will return the Run method's results; on failure
    the final Check will raise the Run method's exception.

    When your DoJobber was configured with no_act=True,
    your Check will always fail since we cannot verify
    if the Run would succeed without actually running it.

    Example Usage:

      class ShellSomething(RunonlyJob):
          def Run(self, *args, **kwargs):
              code = subprocess.call(['/usr/bin/userdel', 'wendellbagg'])
              if code != 1:
                  raise RuntimeError('run failed!')
    """

    _run_err = None

    def Check(self, *_, **__):
        """Fail if Run not run, else return Run result."""
        if self._check_phase == 'check':
            raise RuntimeError(
                'Runonly node check intentionally fails first time.'
            )
        if self._run_exception:
            raise self._run_exception  # pylint:disable=raising-bad-type
        return self._run_results


class DummyJob(Job):
    """A Job with no Check nor Run, always succeeds.

    Useful for creating a node that only has dependencies.
    """

    def Check(self, *dummy_args, **dummy_kwargs):
        """Always pass."""

    def Run(self, *dummy_args, **dummy_kwargs):
        """Always pass."""


class DoJobber:  # pylint:disable=too-many-instance-attributes
    """DoJobber Class."""

    def __init__(self, **kwargs):  # pylint:disable=super-init-not-called
        """Initialization."""
        self.graph = digraph()
        self.nodestatus = {}
        self.nodeexceptions = {}
        self.noderesults = {}
        self._run_phase = 0
        self._retry = {}  # retries left, sleep time, etc
        self._default_tries = None  # num of tries for Jobs that set no value
        self._default_delay = None  # default delay between Job retries
        self._default_retry_delay = None  # min delay between retries of a Job

        self._args = []  # Args for Check/Run methods
        self._kwargs = {}  # KWArgs for Check/Run methods
        self._root = None  # Root Job
        self._checknrun_cwd = None
        self._checknrun_storage = None
        self._classmap = {}  # map of name: actual_class_obj
        self._cleanup = True  # Should we automatically do a cleanup
        self._verbose = False
        self._debug = False
        self._no_act = False
        self._deps = {}
        self._objsrun = []  # which objects ran, for triggering Cleanup

        self._log = logging.getLogger('DoJobber')
        logtarget = logging.StreamHandler()
        logtarget.setFormatter(
            logging.Formatter('DoJobber %(levelname)-19s: %(message)s')
        )
        if kwargs.get('dojobber_loglevel'):
            self._log.setLevel(kwargs['dojobber_loglevel'])
        else:
            self._log.setLevel(logging.CRITICAL)
        self._log.addHandler(logtarget)

    def cleanup(self):
        """Run all Cleanup methods for nodes that ran, LIFO.

        Any Cleanup method that raises an exception will halt
        processing - attempting to be 'smart' and figure out
        which Cleanup problems are safe to ignore is not our job.

        This is called automatically by checknrun at completion
        time, unless you explicitly used cleanup=False in configure()
        """
        for obj in reversed(self._objsrun):
            if callable(getattr(obj, 'Cleanup', None)):
                if self._debug:
                    sys.stderr.write(f'{type(obj).__name__}.cleanup running\n')
                try:
                    obj.Cleanup()
                    if self._debug:
                        sys.stderr.write(
                            f'{type(obj).__name__}.cleanup: pass\n'
                        )
                except Exception as err:
                    sys.stderr.write(
                        f'{type(obj).__name__}.cleanup: fail "{err}"\n'
                    )
                    raise

    def partial_success(self):
        """Returns T/F if any checknrun nodes were succesfull."""
        return self.nodestatus and any(self.nodestatus.values())

    def success(self):
        """Returns T/F if the checknrun hit all nodes with 100% success."""
        return self.nodestatus and all(self.nodestatus.values())

    def failure(self):
        """Returns T/F if the checknrun had any failure nodes."""
        return not self.success()

    def configure(  # pylint:disable=too-many-arguments
        self,
        root,
        no_act=False,
        verbose=False,
        debug=False,
        cleanup=True,
        default_tries=3,
        default_retry_delay=1,
    ):
        """Configure the graph for a specified root Job.

        no_act: only run Checks, do not make changes (i.e. no Run)
        verbose: show a line per check/run/recheck, including recheck
                 failure output
        debug: show full stacktrace on failure of check/run/recheck
        cleanup: run any Cleanup methods on classes once checknrun is complete
        default_tries: number of tries available for each Job if not otherwise
                       specified via the TRIES attribute
        default_retry_delay: min delay between tries of a specific Job if not
                             otherwise specified via the RETRY_DELAY attribute
        """
        self._no_act = no_act
        self._debug = debug
        self._verbose = self._debug or verbose
        self._root = root
        self._default_tries = default_tries
        self._default_retry_delay = default_retry_delay
        self._cleanup = cleanup

        self._load_class()

    def set_args(self, *args, **kwargs):
        """Set the arguments that will be sent to all Check/Run methods."""
        self._args = args
        self._kwargs = kwargs

    def _class_name(self, theclass):
        """Returns a class from a class or string rep."""
        if isinstance(theclass, str):
            return theclass
        return theclass.__name__

    def _node_failed(self, nodename, err):
        """Update graph and attributes for failed node."""
        self.graph.add_node_attribute(nodename, ('style', 'filled'))
        self.graph.add_node_attribute(nodename, ('color', 'red'))
        self.nodestatus[nodename] = False
        self.nodeexceptions[nodename] = err

    def _node_succeeded(self, nodename, results):
        """Update graph and attributes for successful node."""
        self.nodestatus[nodename] = True
        self.graph.add_node_attribute(nodename, ('style', 'filled'))
        self.graph.add_node_attribute(nodename, ('color', 'green'))
        self.noderesults[nodename] = results

    def _node_eventually_succeeded(self, nodename, results):
        """Update graph and attributes for eventually successful node."""
        self.nodestatus[nodename] = True
        self.graph.add_node_attribute(nodename, ('style', 'filled'))
        self.graph.add_node_attribute(nodename, ('color', 'darkgreen'))
        self.noderesults[nodename] = results

    def _node_untested(self, nodename):
        """Update graph and attributes for untested node."""
        self.nodestatus[nodename] = None

    def checknrun(self, node=None):
        """Check and run each class.

        This method initializes the storage and launches
        the actual checknrun routines.

        Environmental concerns

          Your routines SHOULD not create any unexpected side effects,
          but there are things that may not be expected and handled.

          Current working directory
            We'll remember where checknrun is called.
            Before each Check and Run, we'll cd back here.
            We'll also cd back here before returning.

          Environment
            We do not currently preserve environment modifications.
            You shouldn't do them. Future versions will sanitize
            between runs.
        """
        self._checknrun_cwd = os.path.realpath(os.curdir)
        self._checknrun_storage = {'__global': {}}
        self.nodestatus = {}

        trynum = 0
        while True:
            self._checknrun(node)
            if self.success():
                break
            self._run_phase += 1

            # quit if we're out of tries
            # Only 'False' Jobs (have been tried but failed)
            # are retriable - others were either successful or
            # are blocked by other failed Jobs.
            # pylint: disable=consider-using-dict-items
            retriable = [
                f'{x} => {self.nodestatus[x]}'
                for x in self._retry
                if self.nodestatus[x] is False and self._retry[x]['tries'] > 0
            ]
            if not retriable:
                break

            # Do not retry at all in no-act mode
            if self._no_act:
                break

            trynum += 1

        os.chdir(self._checknrun_cwd)
        if self._cleanup:
            self.cleanup()

    def _checknrun(
        self, node=None
    ):  # pylint:disable=too-many-branches,too-many-statements
        """Check and run each class.

        Assumes all storage and other initialization is complete already.
        """
        # pylint:disable=protected-access

        if not node:
            node = self._root
        nodename = self._class_name(node)
        _, _, post = depth_first_search(self.graph, root=nodename)

        # Run dependent nodes and see if all were successful
        blocked = False
        for depnode in post:
            if depnode == nodename:
                continue

            # Run dependent nodes if not already successful
            if not self.nodestatus.get(depnode):
                self._checknrun(depnode)

            # Were all dependent nodes happy?
            if not self.nodestatus.get(depnode):
                blocked = True

        # Set as untested if not visited yet
        if nodename not in self.nodestatus:
            self._node_untested(nodename)

        if not blocked:
            if not self._run_phase > self._retry[nodename]['lastphase']:
                # Already tried - skip
                return
            if not self._retry[nodename]['tries']:
                # Too many tries - abort
                return
            sleeptime = self._retry[nodename]['nexttry'] - time.time()
            if sleeptime > 0:
                time.sleep(sleeptime)
            self._retry[nodename]['nexttry'] = (
                time.time() + self._retry[nodename]['retry_delay']
            )
            self._retry[nodename]['lastphase'] = self._run_phase
            self._retry[nodename]['tries'] -= 1

            try:
                obj = self._classmap[nodename]()
            except Exception as err:  # pylint: disable=broad-except
                self._log.error(
                    'Could not create Job "%s" - check its __init__', nodename
                )
                if self._verbose:
                    sys.stderr.write(f'{nodename}.check: fail\n')
                if self._debug:
                    sys.stderr.write(
                        '  Could not create job, error was {}\n'.format(
                            traceback.format_exc().strip().replace('\n', '\n  ')
                        )
                    )
                self._node_failed(nodename, err)
                return

            self._checknrun_storage[nodename] = {}
            obj._set_storage(
                self._checknrun_storage[nodename],
                self._checknrun_storage['__global'],
            )
            self._objsrun.append(obj)

            # check / run / check
            try:
                os.chdir(self._checknrun_cwd)
                obj._check_phase = 'check'
                obj._check_results = obj.Check(*self._args, **self._kwargs)
                self._node_succeeded(nodename, obj._check_results)
                if self._verbose:
                    sys.stderr.write(f'{nodename}.check: pass\n')
            except Exception as err:  # pylint:disable=broad-except
                obj._check_exception = err
                if self._verbose:
                    sys.stderr.write(f'{nodename}.check: fail\n')

                # In no_act mode, we only run the first check
                # and get out of dodge.
                if self._no_act:
                    self._node_failed(nodename, err)
                    if self._debug:
                        sys.stderr.write(
                            '  Error was:\n  '
                            '{}\n'.format(
                                traceback.format_exc()
                                .strip()
                                .replace('\n', '\n  ')
                            )
                        )
                    return

                # Run the Run method, which may fail with
                # wild abandon - we'll be doing a recheck anyway.
                try:
                    os.chdir(self._checknrun_cwd)
                    obj._run_results = obj.Run(*self._args, **self._kwargs)
                    if self._verbose:
                        sys.stderr.write(f'{nodename}.run: pass\n')
                except Exception as err:  # pylint:disable=W0718,W0621
                    obj._run_exception = err
                    if self._verbose:
                        sys.stderr.write(f'{nodename}.run: fail\n')
                    if self._debug:
                        sys.stderr.write(
                            '  Error was:\n  '
                            '{}\n'.format(
                                traceback.format_exc()
                                .strip()
                                .replace('\n', '\n  ')
                            )
                        )

                # Do a recheck
                try:
                    os.chdir(self._checknrun_cwd)
                    obj._check_phase = 'recheck'
                    obj._recheck_results = obj.Check(
                        *self._args, **self._kwargs
                    )
                    self._node_eventually_succeeded(
                        nodename, obj._recheck_results
                    )
                    if self._verbose:
                        sys.stderr.write(f'{nodename}.recheck: pass\n')
                except Exception as err:  # pylint:disable=broad-except
                    obj._recheck_exception = err
                    if self._verbose:
                        sys.stderr.write(f'{nodename}.recheck: fail "{err}"\n')
                    if self._debug:
                        sys.stderr.write(
                            '  Error was:\n  {}\n'.format(
                                traceback.format_exc()
                                .strip()
                                .replace('\n', '\n  ')
                            )
                        )
                    self._node_failed(nodename, err)

    def _load_class(self):
        """Generate internal graph for a checkrun class."""
        self._init_deps(self._root)
        self._init_graph()

    def _dot_output(self, fmt='png'):
        """Run dot with specified output format and return output."""

        command = ['dot', f'-T{fmt}']
        with subprocess.Popen(
            command, stdin=subprocess.PIPE, stdout=subprocess.PIPE
        ) as proc:
            stdout, _ = proc.communicate(dot.write(self.graph).encode())

            if proc.returncode != 0:
                raise RuntimeError(
                    f'Cannot create dot graphs via {" ".join(command)}'
                )

        return stdout

    def write_graph(self, filed, fmt='png'):
        """Write a graph to the filedescriptor with named format.

        Format must be something understood as a 'dot -Tfmt' argument.

        Raises Error on dot command failure.
        """
        if not dot:
            return

        filed.write(self._dot_output(fmt))

    def display_graph(self):
        """Show the dot graph to X11 screen."""
        if not all((dot, DISPLAY)):
            return

        image_content = self._dot_output()
        with subprocess.Popen(['display'], stdin=subprocess.PIPE) as proc:
            proc.communicate(image_content)
            if proc.returncode != 0:
                raise RuntimeError('Cannot show graph using "display"')

    def _init_graph(self):
        """Initialize our graph."""
        for classname in self._classmap:
            for dep in self._deps[classname]:
                self.graph.add_edge((classname, dep))
        if cycles.find_cycle(self.graph):
            raise RuntimeError(
                'Programmer error: graph contains cycles "'
                f'{cycles.find_cycle(self.graph)}"'
            )

    def _init_deps(self, theclass):
        """Initialize our dependencies."""

        classname = self._class_name(theclass)
        self._log.debug('processing dependencies for %s', classname)
        if classname in self._classmap:
            self._log.debug(' already processed %s', classname)
            return

        self._classmap[classname] = theclass
        self.graph.add_node(classname)
        deps = getattr(theclass, 'DEPS', [])
        tries = (
            getattr(theclass, 'TRIES')
            if getattr(theclass, 'TRIES', None) is not None
            else self._default_tries
        )
        delay = (
            getattr(theclass, 'RETRY_DELAY')
            if getattr(theclass, 'RETRY_DELAY', None) is not None
            else self._default_retry_delay
        )
        if delay < 0:
            raise RuntimeError(f'RETRY_DELAY "{delay}" cannot be negative')
        if int(tries) < 1:
            raise RuntimeError(f'TRIES "{tries}" must be >= 1.')

        self._retry[classname] = {
            # How many more tries we can have
            'tries': tries,
            # How long to wait between retries
            'retry_delay': delay,
            # How soon we can do the next try
            'nexttry': time.time(),
            # In which phase did we do our most recent try
            'lastphase': -1,
        }

        # Check for common error of DEPS being a single
        # thing, not iterable, so we can alert programmer
        try:
            iter(deps)
        except TypeError:
            self._log.critical(
                'DEPS for %s is not iterable; is "%s"', classname, deps
            )
            raise

        for dep in deps:
            self._init_deps(dep)

        self._deps[classname] = [self._class_name(x) for x in deps]


if __name__ == '__main__':
    sys.exit('This is a library only')
