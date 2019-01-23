import datetime
import uuid
from unittest.mock import MagicMock

import pytest

import prefect
from prefect.client.client import Client, FlowRunInfoResult, TaskRunInfoResult
from prefect.engine.result_handlers import ResultHandler
from prefect.engine.cloud import CloudFlowRunner, CloudTaskRunner

from prefect.engine.state import (
    Failed,
    Finished,
    Pending,
    Running,
    Skipped,
    Retrying,
    Success,
    TimedOut,
    TriggerFailed,
)
from prefect.utilities.configuration import set_temporary_config

from collections import namedtuple


class FlowRun:
    def __init__(self, id, state=None, version=None):
        self.id = id
        self.state = state or Pending()
        self.version = version or 0


class TaskRun:
    def __init__(
        self, id, flow_run_id, task_id, state=None, version=None, map_index=None
    ):
        self.id = id
        self.flow_run_id = flow_run_id
        self.task_id = task_id
        self.state = state or Pending()
        self.version = version or 0
        self.map_index = map_index if map_index is not None else -1


@prefect.task
def plus_one(x):
    return x + 1


@prefect.task
def invert_fail_once(x):
    try:
        return 1 / x
    except:
        if prefect.context.get("task_run_count", 0) < 2:
            raise
        else:
            return 100


@pytest.fixture(autouse=True)
def cloud_settings():
    with set_temporary_config(
        {
            "cloud.api": "http://my-cloud.foo",
            "cloud.auth_token": "token",
            "engine.flow_runner.default_class": "prefect.engine.cloud.CloudFlowRunner",
            "engine.task_runner.default_class": "prefect.engine.cloud.CloudTaskRunner",
        }
    ):
        yield


class MockedCloudClient(MagicMock):
    def __init__(self, flow_runs, task_runs, monkeypatch):
        super().__init__()
        self.flow_runs = {fr.id: fr for fr in flow_runs}
        self.task_runs = {tr.id: tr for tr in task_runs}

        monkeypatch.setattr(
            "prefect.engine.cloud.task_runner.Client", MagicMock(return_value=self)
        )
        monkeypatch.setattr(
            "prefect.engine.cloud.flow_runner.Client", MagicMock(return_value=self)
        )

    def get_flow_run_info(self, flow_run_id, *args, **kwargs):
        flow_run = self.flow_runs[flow_run_id]
        task_runs = [t for t in self.task_runs.values() if t.flow_run_id == flow_run_id]

        return FlowRunInfoResult(
            parameters={},
            version=flow_run.version,
            state=flow_run.state,
            task_runs=[
                TaskRunInfoResult(
                    id=tr.id, task_id=tr.task_id, version=tr.version, state=tr.state
                )
                for tr in task_runs
            ],
        )

    def get_task_run_info(self, flow_run_id, task_id, map_index, *args, **kwargs):
        """
        Return task run if found, otherwise
        """
        task_run = next(
            (
                t
                for t in self.task_runs.values()
                if t.flow_run_id == flow_run_id
                and t.task_id == task_id
                and t.map_index == map_index
            ),
            None,
        )

        if not task_run:
            task_run = TaskRun(
                id=str(uuid.uuid4()),
                task_id=task_id,
                flow_run_id=flow_run_id,
                map_index=map_index,
            )
            self.task_runs[task_run.id] = task_run

        return TaskRunInfoResult(
            id=task_run.id,
            task_id=task_id,
            version=task_run.version,
            state=task_run.state,
        )

    def set_flow_run_state(self, flow_run_id, version, state, **kwargs):
        fr = self.flow_runs[flow_run_id]
        if fr.version == version:
            fr.state = state
            fr.version += 1
        else:
            raise ValueError("Invalid flow run update")

    def set_task_run_state(self, task_run_id, version, state, **kwargs):
        tr = self.task_runs[task_run_id]
        if tr.version == version:
            tr.state = state
            tr.version += 1
        else:
            raise ValueError("Invalid task run update")


def test_simple_two_task_flow(monkeypatch):
    flow_run_id = str(uuid.uuid4())
    task_run_id_1 = str(uuid.uuid4())
    task_run_id_2 = str(uuid.uuid4())

    with prefect.Flow() as flow:
        t1 = prefect.Task()
        t2 = prefect.Task()
        t2.set_upstream(t1)

    client = MockedCloudClient(
        flow_runs=[FlowRun(id=flow_run_id)],
        task_runs=[
            TaskRun(id=task_run_id_1, task_id=t1.id, flow_run_id=flow_run_id),
            TaskRun(id=task_run_id_2, task_id=t2.id, flow_run_id=flow_run_id),
        ],
        monkeypatch=monkeypatch,
    )

    with prefect.context(flow_run_id=flow_run_id):
        state = CloudFlowRunner(flow=flow).run(return_tasks=flow.tasks)

    assert state.is_successful()
    assert client.flow_runs[flow_run_id].state.is_successful()
    assert client.task_runs[task_run_id_1].state.is_successful()
    assert client.task_runs[task_run_id_1].version == 2
    assert client.task_runs[task_run_id_2].state.is_successful()


def test_simple_two_task_flow_with_final_task_set_to_fail(monkeypatch):

    flow_run_id = str(uuid.uuid4())
    task_run_id_1 = str(uuid.uuid4())
    task_run_id_2 = str(uuid.uuid4())

    with prefect.Flow() as flow:
        t1 = prefect.Task()
        t2 = prefect.Task()
        t2.set_upstream(t1)

    client = MockedCloudClient(
        flow_runs=[FlowRun(id=flow_run_id)],
        task_runs=[
            TaskRun(id=task_run_id_1, task_id=t1.id, flow_run_id=flow_run_id),
            TaskRun(
                id=task_run_id_2, task_id=t2.id, flow_run_id=flow_run_id, state=Failed()
            ),
        ],
        monkeypatch=monkeypatch,
    )

    with prefect.context(flow_run_id=flow_run_id):
        state = CloudFlowRunner(flow=flow).run(return_tasks=flow.tasks)

    assert state.is_failed()
    assert client.flow_runs[flow_run_id].state.is_failed()
    assert client.task_runs[task_run_id_1].state.is_successful()
    assert client.task_runs[task_run_id_1].version == 2
    assert client.task_runs[task_run_id_2].state.is_failed()
    assert client.task_runs[task_run_id_2].version == 0


def test_simple_two_task_flow_with_final_task_already_running(monkeypatch):

    flow_run_id = str(uuid.uuid4())
    task_run_id_1 = str(uuid.uuid4())
    task_run_id_2 = str(uuid.uuid4())

    with prefect.Flow() as flow:
        t1 = prefect.Task()
        t2 = prefect.Task()
        t2.set_upstream(t1)

    client = MockedCloudClient(
        flow_runs=[FlowRun(id=flow_run_id)],
        task_runs=[
            TaskRun(id=task_run_id_1, task_id=t1.id, flow_run_id=flow_run_id),
            TaskRun(
                id=task_run_id_2,
                task_id=t2.id,
                version=1,
                flow_run_id=flow_run_id,
                state=Running(),
            ),
        ],
        monkeypatch=monkeypatch,
    )

    with prefect.context(flow_run_id=flow_run_id):
        state = CloudFlowRunner(flow=flow).run(return_tasks=flow.tasks)

    assert state.is_running()
    assert client.flow_runs[flow_run_id].state.is_running()
    assert client.task_runs[task_run_id_1].state.is_successful()
    assert client.task_runs[task_run_id_1].version == 2
    assert client.task_runs[task_run_id_2].state.is_running()
    assert client.task_runs[task_run_id_2].version == 1


def test_simple_three_task_flow_with_one_failing_task(monkeypatch):
    @prefect.task
    def error():
        1 / 0

    flow_run_id = str(uuid.uuid4())
    task_run_id_1 = str(uuid.uuid4())
    task_run_id_2 = str(uuid.uuid4())
    task_run_id_3 = str(uuid.uuid4())

    with prefect.Flow() as flow:
        t1 = prefect.Task()
        t2 = prefect.Task()
        t3 = error()
        t2.set_upstream(t1)
        t3.set_upstream(t2)

    client = MockedCloudClient(
        flow_runs=[FlowRun(id=flow_run_id)],
        task_runs=[
            TaskRun(id=task_run_id_1, task_id=t1.id, flow_run_id=flow_run_id),
            TaskRun(id=task_run_id_2, task_id=t2.id, flow_run_id=flow_run_id),
            TaskRun(id=task_run_id_3, task_id=t3.id, flow_run_id=flow_run_id),
        ],
        monkeypatch=monkeypatch,
    )

    with prefect.context(flow_run_id=flow_run_id):
        state = CloudFlowRunner(flow=flow).run(return_tasks=flow.tasks)

    assert state.is_failed()
    assert client.flow_runs[flow_run_id].state.is_failed()
    assert client.task_runs[task_run_id_1].state.is_successful()
    assert client.task_runs[task_run_id_1].version == 2
    assert client.task_runs[task_run_id_2].state.is_successful()
    assert client.task_runs[task_run_id_2].version == 2
    assert client.task_runs[task_run_id_3].state.is_failed()
    assert client.task_runs[task_run_id_2].version == 2


def test_simple_map(monkeypatch):

    flow_run_id = str(uuid.uuid4())
    task_run_id_1 = str(uuid.uuid4())

    with prefect.Flow() as flow:
        t1 = plus_one.map([0, 1, 2])

    client = MockedCloudClient(
        flow_runs=[FlowRun(id=flow_run_id)],
        task_runs=[TaskRun(id=task_run_id_1, task_id=t1.id, flow_run_id=flow_run_id)]
        + [
            TaskRun(id=t.id, task_id=t.id, flow_run_id=flow_run_id)
            for t in flow.tasks
            if t is not t1
        ],
        monkeypatch=monkeypatch,
    )

    with prefect.context(flow_run_id=flow_run_id):
        state = CloudFlowRunner(flow=flow).run(return_tasks=flow.tasks)

    assert state.is_successful()
    assert client.flow_runs[flow_run_id].state.is_successful()
    assert client.task_runs[task_run_id_1].state.is_mapped()
    # there should be a total of 4 task runs corresponding to the mapped task
    assert len([tr for tr in client.task_runs.values() if tr.task_id == t1.id]) == 4


def test_deep_map(monkeypatch):

    flow_run_id = str(uuid.uuid4())
    task_run_id_1 = str(uuid.uuid4())
    task_run_id_2 = str(uuid.uuid4())
    task_run_id_3 = str(uuid.uuid4())

    with prefect.Flow() as flow:
        t1 = plus_one.map([0, 1, 2])
        t2 = plus_one.map(t1)
        t3 = plus_one.map(t2)

    client = MockedCloudClient(
        flow_runs=[FlowRun(id=flow_run_id)],
        task_runs=[
            TaskRun(id=task_run_id_1, task_id=t1.id, flow_run_id=flow_run_id),
            TaskRun(id=task_run_id_2, task_id=t2.id, flow_run_id=flow_run_id),
            TaskRun(id=task_run_id_3, task_id=t3.id, flow_run_id=flow_run_id),
        ]
        + [
            TaskRun(id=t.id, task_id=t.id, flow_run_id=flow_run_id)
            for t in flow.tasks
            if t not in [t1, t2, t3]
        ],
        monkeypatch=monkeypatch,
    )

    with prefect.context(flow_run_id=flow_run_id):
        state = CloudFlowRunner(flow=flow).run(return_tasks=flow.tasks)

    assert state.is_successful()
    assert client.flow_runs[flow_run_id].state.is_successful()
    assert client.task_runs[task_run_id_1].state.is_mapped()
    assert client.task_runs[task_run_id_2].state.is_mapped()
    assert client.task_runs[task_run_id_3].state.is_mapped()

    # there should be a total of 4 task runs corresponding to each mapped task
    for t in [t1, t2, t3]:
        assert len([tr for tr in client.task_runs.values() if tr.task_id == t.id]) == 4


def test_deep_map_with_a_failure(monkeypatch):

    flow_run_id = str(uuid.uuid4())
    task_run_id_1 = str(uuid.uuid4())
    task_run_id_2 = str(uuid.uuid4())
    task_run_id_3 = str(uuid.uuid4())

    with prefect.Flow() as flow:
        t1 = plus_one.map([-1, 0, 1])
        t2 = invert_fail_once.map(t1)
        t3 = plus_one.map(t2)

    client = MockedCloudClient(
        flow_runs=[FlowRun(id=flow_run_id)],
        task_runs=[
            TaskRun(id=task_run_id_1, task_id=t1.id, flow_run_id=flow_run_id),
            TaskRun(id=task_run_id_2, task_id=t2.id, flow_run_id=flow_run_id),
            TaskRun(id=task_run_id_3, task_id=t3.id, flow_run_id=flow_run_id),
        ]
        + [
            TaskRun(id=t.id, task_id=t.id, flow_run_id=flow_run_id)
            for t in flow.tasks
            if t not in [t1, t2, t3]
        ],
        monkeypatch=monkeypatch,
    )

    with prefect.context(flow_run_id=flow_run_id):
        state = CloudFlowRunner(flow=flow).run(return_tasks=flow.tasks)

    assert state.is_failed()
    assert client.flow_runs[flow_run_id].state.is_failed()
    assert client.task_runs[task_run_id_1].state.is_mapped()
    assert client.task_runs[task_run_id_2].state.is_mapped()
    assert client.task_runs[task_run_id_3].state.is_mapped()

    # there should be a total of 4 task runs corresponding to each mapped task
    for t in [t1, t2, t3]:
        assert len([tr for tr in client.task_runs.values() if tr.task_id == t.id]) == 4

    # t2's first child task should have failed
    t2_0 = next(
        tr
        for tr in client.task_runs.values()
        if tr.task_id == t2.id and tr.map_index == 0
    )
    assert t2_0.state.is_failed()

    # t3's first child task should have failed
    t3_0 = next(
        tr
        for tr in client.task_runs.values()
        if tr.task_id == t3.id and tr.map_index == 0
    )
    assert t3_0.state.is_failed()


@pytest.mark.xfail(reason="statefulness errors on second run with dask executors")
def test_deep_map_with_a_retry(monkeypatch):

    flow_run_id = str(uuid.uuid4())
    task_run_id_1 = str(uuid.uuid4())
    task_run_id_2 = str(uuid.uuid4())
    task_run_id_3 = str(uuid.uuid4())

    with prefect.Flow() as flow:
        t1 = plus_one.map([-1, 0, 1])
        t2 = invert_fail_once.map(t1)
        t3 = plus_one.map(t2)

    t2.max_retries = 1
    t2.retry_delay = datetime.timedelta(seconds=0)

    client = MockedCloudClient(
        flow_runs=[FlowRun(id=flow_run_id)],
        task_runs=[
            TaskRun(id=task_run_id_1, task_id=t1.id, flow_run_id=flow_run_id),
            TaskRun(id=task_run_id_2, task_id=t2.id, flow_run_id=flow_run_id),
            TaskRun(id=task_run_id_3, task_id=t3.id, flow_run_id=flow_run_id),
        ]
        + [
            TaskRun(id=t.id, task_id=t.id, flow_run_id=flow_run_id)
            for t in flow.tasks
            if t not in [t1, t2, t3]
        ],
        monkeypatch=monkeypatch,
    )

    with prefect.context(flow_run_id=flow_run_id):
        CloudFlowRunner(flow=flow).run()

    assert client.flow_runs[flow_run_id].state.is_running()
    assert client.task_runs[task_run_id_1].state.is_mapped()
    assert client.task_runs[task_run_id_2].state.is_mapped()
    assert client.task_runs[task_run_id_3].state.is_mapped()

    # there should be a total of 4 task runs corresponding to each mapped task
    for t in [t1, t2, t3]:
        assert len([tr for tr in client.task_runs.values() if tr.task_id == t.id]) == 4

    # t2's first child task should be retrying
    t2_0 = next(
        tr
        for tr in client.task_runs.values()
        if tr.task_id == t2.id and tr.map_index == 0
    )
    assert isinstance(t2_0.state, Retrying)

    # t3's first child task should be pending
    t3_0 = next(
        tr
        for tr in client.task_runs.values()
        if tr.task_id == t3.id and tr.map_index == 0
    )
    assert t3_0.state.is_pending()

    # RUN A SECOND TIME
    with prefect.context(flow_run_id=flow_run_id):
        CloudFlowRunner(flow=flow).run()

    # t2's first child task should be successful
    t2_0 = next(
        tr
        for tr in client.task_runs.values()
        if tr.task_id == t2.id and tr.map_index == 0
    )
    assert t2_0.state.is_successful()

    # t3's first child task should be successful
    t3_0 = next(
        tr
        for tr in client.task_runs.values()
        if tr.task_id == t3.id and tr.map_index == 0
    )
    assert t3_0.state.is_successful()
