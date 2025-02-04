import pytest

from distributed import Worker, WorkerPlugin
from distributed.utils_test import async_wait_for, gen_cluster, inc


class MyPlugin(WorkerPlugin):
    name = "MyPlugin"

    def __init__(self, data, expected_notifications=None):
        self.data = data
        self.expected_notifications = expected_notifications

    def setup(self, worker):
        assert isinstance(worker, Worker)
        self.worker = worker
        self.worker._my_plugin_status = "setup"
        self.worker._my_plugin_data = self.data

        self.observed_notifications = []

    def teardown(self, worker):
        self.worker._my_plugin_status = "teardown"

        if self.expected_notifications is not None:
            assert len(self.observed_notifications) == len(self.expected_notifications)
            for expected, real in zip(
                self.expected_notifications, self.observed_notifications
            ):
                assert expected == real

    def transition(self, key, start, finish, **kwargs):
        self.observed_notifications.append(
            {"key": key, "start": start, "finish": finish}
        )

    def release_key(self, key, state, cause, reason, report):
        self.observed_notifications.append({"key": key, "state": state})


@gen_cluster(client=True, nthreads=[])
async def test_create_with_client(c, s):
    await c.register_worker_plugin(MyPlugin(123))

    worker = await Worker(s.address, loop=s.loop)
    assert worker._my_plugin_status == "setup"
    assert worker._my_plugin_data == 123

    await worker.close()
    assert worker._my_plugin_status == "teardown"


@gen_cluster(client=True, nthreads=[])
async def test_remove_with_client(c, s):
    await c.register_worker_plugin(MyPlugin(123), name="foo")
    await c.register_worker_plugin(MyPlugin(546), name="bar")

    worker = await Worker(s.address, loop=s.loop)
    # remove the 'foo' plugin
    await c.unregister_worker_plugin("foo")
    assert worker._my_plugin_status == "teardown"

    # check that on the scheduler registered worker plugins we only have 'bar'
    assert len(s.worker_plugins) == 1
    assert "bar" in s.worker_plugins

    # check on the worker plugins that we only have 'bar'
    assert len(worker.plugins) == 1
    assert "bar" in worker.plugins

    # let's remove 'bar' and we should have none worker plugins
    await c.unregister_worker_plugin("bar")
    assert worker._my_plugin_status == "teardown"
    assert not s.worker_plugins
    assert not worker.plugins


@gen_cluster(client=True, nthreads=[])
async def test_remove_with_client_raises(c, s):
    await c.register_worker_plugin(MyPlugin(123), name="foo")

    worker = await Worker(s.address, loop=s.loop)
    with pytest.raises(ValueError, match="bar"):
        await c.unregister_worker_plugin("bar")


@gen_cluster(client=True, nthreads=[])
async def test_create_with_client_and_plugin_from_class(c, s):
    await c.register_worker_plugin(MyPlugin, data=456)

    worker = await Worker(s.address, loop=s.loop)
    assert worker._my_plugin_status == "setup"
    assert worker._my_plugin_data == 456

    # Give the plugin a new name so that it registers
    await c.register_worker_plugin(MyPlugin, name="new", data=789)
    assert worker._my_plugin_data == 789


@gen_cluster(client=True, worker_kwargs={"plugins": [MyPlugin(5)]})
async def test_create_on_construction(c, s, a, b):
    assert len(a.plugins) == len(b.plugins) == 1
    assert a._my_plugin_status == "setup"
    assert a._my_plugin_data == 5


@gen_cluster(nthreads=[("127.0.0.1", 1)], client=True)
async def test_normal_task_transitions_called(c, s, w):
    expected_notifications = [
        {"key": "task", "start": "new", "finish": "waiting"},
        {"key": "task", "start": "waiting", "finish": "ready"},
        {"key": "task", "start": "ready", "finish": "executing"},
        {"key": "task", "start": "executing", "finish": "memory"},
        {"key": "task", "state": "memory"},
    ]

    plugin = MyPlugin(1, expected_notifications=expected_notifications)

    await c.register_worker_plugin(plugin)
    await c.submit(lambda x: x, 1, key="task")
    await async_wait_for(lambda: not w.tasks, timeout=10)


@gen_cluster(nthreads=[("127.0.0.1", 1)], client=True)
async def test_failing_task_transitions_called(c, s, w):
    def failing(x):
        raise Exception()

    expected_notifications = [
        {"key": "task", "start": "new", "finish": "waiting"},
        {"key": "task", "start": "waiting", "finish": "ready"},
        {"key": "task", "start": "ready", "finish": "executing"},
        {"key": "task", "start": "executing", "finish": "error"},
        {"key": "task", "state": "error"},
    ]

    plugin = MyPlugin(1, expected_notifications=expected_notifications)

    await c.register_worker_plugin(plugin)

    with pytest.raises(Exception):
        await c.submit(failing, 1, key="task")


@gen_cluster(
    nthreads=[("127.0.0.1", 1)], client=True, worker_kwargs={"resources": {"X": 1}}
)
async def test_superseding_task_transitions_called(c, s, w):
    expected_notifications = [
        {"key": "task", "start": "new", "finish": "waiting"},
        {"key": "task", "start": "waiting", "finish": "constrained"},
        {"key": "task", "start": "constrained", "finish": "executing"},
        {"key": "task", "start": "executing", "finish": "memory"},
        {"key": "task", "state": "memory"},
    ]

    plugin = MyPlugin(1, expected_notifications=expected_notifications)

    await c.register_worker_plugin(plugin)
    await c.submit(lambda x: x, 1, key="task", resources={"X": 1})
    await async_wait_for(lambda: not w.tasks, timeout=10)


@gen_cluster(nthreads=[("127.0.0.1", 1)], client=True)
async def test_dependent_tasks(c, s, w):
    dsk = {"dep": 1, "task": (inc, "dep")}

    expected_notifications = [
        {"key": "dep", "start": "new", "finish": "waiting"},
        {"key": "dep", "start": "waiting", "finish": "ready"},
        {"key": "dep", "start": "ready", "finish": "executing"},
        {"key": "dep", "start": "executing", "finish": "memory"},
        {"key": "task", "start": "new", "finish": "waiting"},
        {"key": "task", "start": "waiting", "finish": "ready"},
        {"key": "task", "start": "ready", "finish": "executing"},
        {"key": "task", "start": "executing", "finish": "memory"},
        {"key": "dep", "state": "memory"},
        {"key": "task", "state": "memory"},
    ]

    plugin = MyPlugin(1, expected_notifications=expected_notifications)

    await c.register_worker_plugin(plugin)
    await c.get(dsk, "task", sync=False)
    await async_wait_for(lambda: not w.tasks, timeout=10)


@gen_cluster(nthreads=[("127.0.0.1", 1)], client=True)
async def test_registering_with_name_arg(c, s, w):
    class FooWorkerPlugin:
        def setup(self, worker):
            if hasattr(worker, "foo"):
                raise RuntimeError(f"Worker {worker.address} already has foo!")

            worker.foo = True

    responses = await c.register_worker_plugin(FooWorkerPlugin(), name="foo")
    assert list(responses.values()) == [{"status": "OK"}]

    async with Worker(s.address, loop=s.loop):
        responses = await c.register_worker_plugin(FooWorkerPlugin(), name="foo")
        assert list(responses.values()) == [{"status": "repeat"}] * 2


@gen_cluster(nthreads=[("127.0.0.1", 1)], client=True)
async def test_empty_plugin(c, s, w):
    class EmptyPlugin:
        pass

    await c.register_worker_plugin(EmptyPlugin())
