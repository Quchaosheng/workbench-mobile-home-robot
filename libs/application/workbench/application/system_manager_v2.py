"""APP1: Production system manager.

Startup is transactional. A graph that is half started is worse than one that
never started: the running components keep holding ports, threads and device
handles while the manager reports a failure, and a retry re-enters the partially
started graph. Every component that did start is therefore stopped again, in
reverse order, before the failure is reported.
"""

import logging
import threading
from enum import Enum

logger = logging.getLogger("SystemManager")


class ComponentState(Enum):
    CREATED = "created"
    RUNNING = "running"
    ERROR = "error"
    STOPPED = "stopped"


class SystemManager:
    """Thread-safe lifecycle management"""

    def __init__(self):
        self.components = {}
        self.lock = threading.RLock()
        self.state = "initialized"
        # Operator diagnostics for a failed rollback or shutdown. Held on the
        # manager rather than only in the log, because the log may be rotated
        # before anyone reads it.
        self.rollback_failures: list[tuple[str, str]] = []
        self.last_failed_component: str | None = None
        # Names in the order they reached RUNNING during the last startup. After
        # a failed startup these are the components that were rolled back, so the
        # failure can be reported exactly rather than reconstructed.
        self.started_components: list[str] = []

    def register(self, name: str, instance) -> bool:
        with self.lock:
            if name in self.components:
                return False
            self.components[name] = {"state": ComponentState.CREATED, "instance": instance}
            logger.info(f"Registered: {name}")
            return True

    def _release(self, name: str, component: dict) -> bool:
        """Call one component's shutdown hook and record the released state.

        The component that failed startup is released too, because it may have
        acquired resources before reporting failure. Its own name stays in
        `last_failed_component` and `started_components` records what was rolled
        back, so the ERROR marker is not needed to identify it.
        """
        try:
            if hasattr(component["instance"], "shutdown"):
                component["instance"].shutdown()
        except Exception as error:  # noqa: BLE001 - cleanup must not abort cleanup
            component["state"] = ComponentState.ERROR
            self.rollback_failures.append((name, str(error)))
            logger.error(f"Shutdown failed for component {name}: {error}")
            return False
        component["state"] = ComponentState.STOPPED
        return True

    def startup(self) -> bool:
        with self.lock:
            if self.state == "running":
                # Repeating a successful startup must not start anything twice.
                return True

            self.rollback_failures = []
            self.last_failed_component = None
            self.started_components = []
            started: list[str] = []
            for name, component in self.components.items():
                if hasattr(component["instance"], "startup"):
                    try:
                        start_ok = bool(component["instance"].startup())
                    except Exception as error:  # noqa: BLE001 - a raising component is a failed component
                        logger.error(f"Component {name} raised during startup: {error}")
                        start_ok = False
                else:
                    start_ok = True

                if not start_ok:
                    component["state"] = ComponentState.ERROR
                    self.last_failed_component = name
                    logger.error(f"Component {name} failed to start; rolling back {len(started)} component(s)")
                    # The failing component is released first: it may have
                    # acquired resources before returning False. Then the
                    # successful ones in reverse, so a dependent releases before
                    # its dependency.
                    self._release(name, component)
                    for started_name in reversed(started):
                        self._release(started_name, self.components[started_name])
                    self.started_components = started
                    self.state = "failed"
                    return False

                component["state"] = ComponentState.RUNNING
                started.append(name)

            self.started_components = started
            self.state = "running"
            logger.info("System started")
            return True

    def shutdown(self) -> bool:
        with self.lock:
            # A component still in CREATED never had startup() called, so it has
            # nothing to release and must not receive a shutdown it never paired
            # with. RUNNING and ERROR components may hold resources: a component
            # that failed part-way through startup still needs its cleanup hook.
            pending = [
                (name, component)
                for name, component in self.components.items()
                if component["state"] in {ComponentState.RUNNING, ComponentState.ERROR}
            ]
            for _name, component in self.components.items():
                if component["state"] is ComponentState.CREATED:
                    component["state"] = ComponentState.STOPPED
            if not pending:
                # Repeating a completed shutdown must not stop anything twice.
                self.state = "stopped"
                return not self.rollback_failures

            failed = False
            for name, component in reversed(pending):
                if not self._release(name, component):
                    failed = True

            # Only claim "stopped" when nothing is left holding a resource.
            self.state = "stopped" if not failed else "failed"
            return not failed
