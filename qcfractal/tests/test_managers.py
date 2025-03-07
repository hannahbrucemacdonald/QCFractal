"""
Explicit tests for queue manipulation.
"""

import contextlib
import logging
import re
import time
from concurrent.futures import ProcessPoolExecutor

import pytest

import qcfractal.interface as ptl
from qcfractal import FractalServer, queue, testing
from qcfractal.testing import reset_server_database, test_server, mark_slow

CLIENT_USERNAME = "test_compute_adapter"


@contextlib.contextmanager
def caplog_handler_at_level(caplog_fixture, level, logger=None):
    """
    Helper function to set the caplog fixture's handler to a certain level as well, otherwise it wont be captured

    e.g. if caplog.set_level(logging.INFO) but caplog.handler is at logging.CRITICAL, anything below CRITICAL wont be
    captured.
    """
    starting_handler_level = caplog_fixture.handler.level
    caplog_fixture.handler.setLevel(level)
    with caplog_fixture.at_level(level, logger=logger):
        yield
    caplog_fixture.handler.setLevel(starting_handler_level)


@pytest.fixture(scope="module")
def compute_adapter_fixture(test_server):

    client = ptl.FractalClient(test_server, username=CLIENT_USERNAME)

    with ProcessPoolExecutor(max_workers=2) as adapter:

        yield client, test_server, adapter


@testing.using_rdkit
def test_queue_manager_single_tags(compute_adapter_fixture):
    client, server, adapter = compute_adapter_fixture
    reset_server_database(server)

    manager_stuff = queue.QueueManager(client, adapter, queue_tag="stuff")
    manager_other = queue.QueueManager(client, adapter, queue_tag="other")

    # Add compute
    hooh = ptl.data.get_molecule("hooh.json")
    ret = client.add_compute("rdkit", "UFF", "", "energy", None, [hooh], tag="other")

    # Computer with the incorrect tag
    manager_stuff.await_results()
    ret = client.query_results()
    assert len(ret) == 0

    # Computer with the correct tag
    manager_other.await_results()
    ret = client.query_results()
    assert len(ret) == 1

    # Check the logs to make sure
    managers = server.storage.get_managers()["data"]
    assert len(managers) == 2

    test_results = {"stuff": 0, "other": 1}
    for manager in managers:
        value = test_results[manager["tag"]]
        assert manager["submitted"] == value
        assert manager["completed"] == value
        assert manager["username"] == CLIENT_USERNAME


@mark_slow
@testing.using_rdkit
def test_queue_manager_statistics(compute_adapter_fixture, caplog):
    """Test statistics are correctly generated"""
    # Setup manager and add some compute
    client, server, adapter = compute_adapter_fixture
    reset_server_database(server)

    manager = queue.QueueManager(client, adapter, verbose=True)

    hooh = ptl.data.get_molecule("hooh.json")
    client.add_compute("rdkit", "UFF", "", "energy", None, [hooh], tag="other")

    # Set capture level
    with caplog_handler_at_level(caplog, logging.INFO):
        # Pull jobs to manager
        manager.update()
        # Tasks should not have been started yet
        assert "Task statistics unavailable" in caplog.text
        assert "Task Stats: Processed" not in caplog.text
        manager.await_results()
        # Ensure text is at least generated
        assert "Task Stats: Processed" in caplog.text
        assert "Core Usage vs. Max Resources" in caplog.text
        # Ensure some kind of stats are being calculated seemingly correctly
        stats_re = re.search(r'Core Usage Efficiency: (\d+\.\d+)%', caplog.text)
        assert stats_re is not None and float(stats_re.group(1)) != 0.0
    # Clean up capture so it does not flood the output
    caplog.records.clear()
    caplog.handler.records.clear()


@testing.using_rdkit
def test_queue_manager_shutdown(compute_adapter_fixture):
    """Tests to ensure tasks are returned to queue when the manager shuts down
    """
    client, server, adapter = compute_adapter_fixture
    reset_server_database(server)

    manager = queue.QueueManager(client, adapter)

    hooh = ptl.data.get_molecule("hooh.json")
    client.add_compute("rdkit", "UFF", "", "energy", None, [hooh], tag="other")

    # Pull job to manager and shutdown
    manager.update()
    assert len(manager.list_current_tasks()) == 1
    assert manager.shutdown()["nshutdown"] == 1

    sman = server.list_managers(name=manager.name())
    assert len(sman) == 1
    assert sman[0]["status"] == "INACTIVE"

    # Boot new manager and await results
    manager = queue.QueueManager(client, adapter)
    manager.await_results()
    ret = client.query_results()
    assert len(ret) == 1


@testing.using_rdkit
def test_queue_manager_server_delay(compute_adapter_fixture):
    """Test to ensure interrupts to the server shutdown correctly"""
    client, server, adapter = compute_adapter_fixture
    reset_server_database(server)

    manager = queue.QueueManager(client, adapter, server_error_retries=1)

    hooh = ptl.data.get_molecule("hooh.json")
    client.add_compute("rdkit", "UFF", "", "energy", None, [hooh], tag="other")

    # Pull job to manager and shutdown
    manager.update()
    assert len(manager.list_current_tasks()) == 1

    # Mock a network error
    client._mock_network_error = True
    # Let the calculation finish
    manager.queue_adapter.await_results()
    # Try to push the changes through the network error
    manager.update()
    assert len(manager.list_current_tasks()) == 0
    assert len(manager._stale_payload_tracking) == 1
    assert manager.n_stale_jobs == 0

    # Try again to push the tracked attempts into stale
    manager.update()
    assert len(manager.list_current_tasks()) == 0
    assert len(manager._stale_payload_tracking) == 0
    assert manager.n_stale_jobs == 1
    # Update again to push jobs to stale
    manager.update()

    # Return the jobs to the server
    client._mock_network_error = False
    assert manager.shutdown()["nshutdown"] == 1

    # Once more, but this time restart the server in between
    manager = queue.QueueManager(client, adapter, server_error_retries=1)
    manager.update()
    assert len(manager.list_current_tasks()) == 1
    manager.queue_adapter.await_results()
    # Trigger our failure
    client._mock_network_error = True
    manager.update()
    assert len(manager.list_current_tasks()) == 0
    assert len(manager._stale_payload_tracking) == 1
    assert manager.n_stale_jobs == 0
    # Stop mocking a network error
    client._mock_network_error = False
    manager.update()
    assert len(manager.list_current_tasks()) == 0
    assert len(manager._stale_payload_tracking) == 0
    assert manager.n_stale_jobs == 0


def test_queue_manager_heartbeat(compute_adapter_fixture):
    """Tests to ensure tasks are returned to queue when the manager shuts down
    """

    client, server, adapter = compute_adapter_fixture

    with testing.loop_in_thread() as loop:

        # Build server, manually handle IOLoop (no start/stop needed)
        server = FractalServer(port=testing.find_open_port(),
                               storage_project_name=server.storage_database,
                               storage_uri=server.storage_uri,
                               loop=loop,
                               ssl_options=False,
                               heartbeat_frequency=0.1)

        # Clean and re-init the database
        testing.reset_server_database(server)

        client = ptl.FractalClient(server)
        manager = queue.QueueManager(client, adapter)

        sman = server.list_managers(name=manager.name())
        assert len(sman) == 1
        assert sman[0]["status"] == "ACTIVE"

        # Make sure interval exceeds heartbeat time
        time.sleep(1)
        server.check_manager_heartbeats()

        sman = server.list_managers(name=manager.name())
        assert len(sman) == 1
        assert sman[0]["status"] == "INACTIVE"


def test_manager_max_tasks_limiter(compute_adapter_fixture):
    client, server, adapter = compute_adapter_fixture

    manager = queue.QueueManager(client, adapter, queue_tag="stuff", max_tasks=1.e9)
    assert manager.max_tasks < 1.e9


def test_queue_manager_testing():

    with ProcessPoolExecutor(max_workers=2) as adapter:
        manager = queue.QueueManager(None, adapter)

        assert manager.test()
