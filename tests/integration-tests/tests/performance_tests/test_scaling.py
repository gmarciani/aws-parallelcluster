import datetime
import json
import logging
import time

import pytest
from assertpy import assert_that, soft_assertions
from benchmarks.common.metrics_reporter import produce_benchmark_metrics_report
from remote_command_executor import RemoteCommandExecutor
from time_utils import minutes
from utils import disable_protected_mode

from tests.common.assertions import assert_no_msg_in_logs
from tests.common.scaling_common import get_scaling_metrics


@pytest.mark.parametrize(
    "max_nodes",
    [1000],
)
def test_scaling(
    vpc_stack,
    instance,
    os,
    region,
    scheduler,
    pcluster_config_reader,
    clusters_factory,
    test_datadir,
    scheduler_commands_factory,
    max_nodes,
):
    cluster_config = pcluster_config_reader(max_nodes=max_nodes)
    cluster = clusters_factory(cluster_config)

    logging.info("Cluster Created")

    remote_command_executor = RemoteCommandExecutor(cluster)
    scheduler_commands = scheduler_commands_factory(remote_command_executor)

    logging.info(f"Submitting an array of {max_nodes} jobs on {max_nodes} nodes")
    job_id = scheduler_commands.submit_command_and_assert_job_accepted(
        submit_command_args={
            "command": "srun sleep 10",
            "partition": "queue-0",
            "nodes": max_nodes,
            "slots": max_nodes,
        }
    )

    logging.info(f"Waiting for job to be running: {job_id}")
    scheduler_commands.wait_job_running(job_id)
    logging.info(f"Job {job_id} is running")

    logging.info(f"Cancelling job: {job_id}")
    scheduler_commands.cancel_job(job_id)
    logging.info(f"Job {job_id} cancelled")

    logging.info("Verifying no bootstrap errors in logs")
    assert_no_msg_in_logs(
        remote_command_executor,
        log_files=["/var/log/parallelcluster/clustermgtd"],
        log_msg=["Found the following bootstrap failure nodes"],
    )


def _datetime_to_minute(dt: datetime):
    return dt.replace(second=0, microsecond=0)


def _get_scaling_time(capacity_time_series: list, timestamps: list, scaling_target: int, start_time: datetime):
    scaling_target_index = capacity_time_series.index(scaling_target)
    timestamp_at_full_cluster_size = timestamps[scaling_target_index]
    scaling_target_time = datetime.datetime.fromtimestamp(
        float(timestamp_at_full_cluster_size), tz=datetime.timezone.utc
    )
    return scaling_target_time, int((scaling_target_time - start_time).total_seconds())


SCALING_TARGETS = [1000, 2000, 3000, 4000]


@pytest.mark.usefixtures("scheduler")
@pytest.mark.parametrize(
    "max_monitoring_time_in_mins, shared_headnode_storage_type, head_node_instance_type, scaling_strategy",
    [
        (20, "Efs", "c5.24xlarge", "best-effort"),
        # NOTE parallel tests will concur to RunInstances Resource Token Bucket consumption
        (20, "Efs", "c5.24xlarge", "all-or-nothing"),
    ],
)
def test_scaling_stress_test(
    test_datadir,
    instance,
    os,
    region,
    request,
    pcluster_config_reader,
    scheduler_commands_factory,
    clusters_factory,
    max_monitoring_time_in_mins,
    shared_headnode_storage_type,
    head_node_instance_type,
    scaling_strategy,
):
    """
    This test scales a cluster up and down while periodically monitoring some primary metrics.
    The metrics monitored are:
    - Number of EC2 instances launched
    - Number of successfully bootstrapped compute nodes that have joined the cluster
    - Number of jobs pending or in configuration
    - Number of jobs currently running

    The above metrics are uploaded to CloudWatch.
    The output of this test are:
    - Log messages with the Scale up and Scale down time in seconds
    - Log with the Metrics Source that can be used from CloudWatch Console
    - A Metrics Image showing the scale up and scale down using a linear graph with annotations
    """
    # Creating cluster with intended head node instance type and scaling parameters
    cluster_config = pcluster_config_reader(
        # Prevent nodes being set down before we start monitoring the scale down metrics
        scaledown_idletime=max_monitoring_time_in_mins,
        max_cluster_size=max(SCALING_TARGETS),
        head_node_instance_type=head_node_instance_type,
        shared_headnode_storage_type=shared_headnode_storage_type,
        scaling_strategy=scaling_strategy,
    )
    cluster = clusters_factory(cluster_config)
    remote_command_executor = RemoteCommandExecutor(cluster)
    scheduler_commands = scheduler_commands_factory(remote_command_executor)

    # Disable protected mode since bootstrap errors are likely to occur given the large cluster sizes
    disable_protected_mode(remote_command_executor)

    with soft_assertions():
        for scaling_target in SCALING_TARGETS:
            _scale_up_and_down(
                cluster,
                head_node_instance_type,
                instance,
                max_monitoring_time_in_mins,
                os,
                region,
                remote_command_executor,
                request,
                scaling_target,
                scaling_strategy,
                scheduler_commands,
                test_datadir,
            )

            # Make sure the RunInstances Resource Token Bucket is full before starting another scaling up
            # ref https://docs.aws.amazon.com/AWSEC2/latest/APIReference/throttling.html
            if scaling_target != SCALING_TARGETS[-1]:
                logging.info("Waiting for the RunInstances Resource Token Bucket to refill")
                time.sleep(300)


def _scale_up_and_down(
    cluster,
    head_node_instance_type,
    instance,
    max_monitoring_time_in_mins,
    os,
    region,
    remote_command_executor,
    request,
    scaling_target,
    scaling_strategy,
    scheduler_commands,
    test_datadir,
):
    # Reset underlying ssh connection to prevent socket closed error
    remote_command_executor.reset_connection()
    # Make sure partitions are active
    cluster.start(wait_running=True)

    # Submit a simple job to trigger the launch all compute nodes
    scaling_job = {
        # Keep job running until we explicitly cancel it and start monitoring scale down
        "command": f"srun sleep {minutes(max_monitoring_time_in_mins) // 1000}",
        "nodes": scaling_target,
    }
    job_id = scheduler_commands.submit_command_and_assert_job_accepted(scaling_job)
    # Set start time at minute granularity (to simplify calculation and visualising on CloudWatch)
    start_time = _datetime_to_minute(datetime.datetime.now(tz=datetime.timezone.utc))
    # Monitor the cluster during scale up
    ec2_capacity_time_series_up, compute_nodes_time_series_up, timestamps, end_time = get_scaling_metrics(
        remote_command_executor,
        max_monitoring_time=minutes(max_monitoring_time_in_mins),
        region=region,
        cluster_name=cluster.name,
        publish_metrics=True,
        target_cluster_size=scaling_target,
    )
    # Extract scale up duration and timestamp from the monitoring metrics collected above
    _, scale_up_time_ec2 = _get_scaling_time(ec2_capacity_time_series_up, timestamps, scaling_target, start_time)
    scaling_target_time, scale_up_time_scheduler = _get_scaling_time(
        compute_nodes_time_series_up, timestamps, scaling_target, start_time
    )
    # Cancel the running job and scale down the cluster using the update-compute-fleet command
    scheduler_commands.cancel_job(job_id)
    cluster.stop()
    # Monitor the cluster during scale down
    scale_down_start_timestamp = _datetime_to_minute(datetime.datetime.now(tz=datetime.timezone.utc))
    ec2_capacity_time_series_down, compute_nodes_time_series_down, timestamps, end_time = get_scaling_metrics(
        remote_command_executor,
        max_monitoring_time=minutes(max_monitoring_time_in_mins),
        region=region,
        cluster_name=cluster.name,
        publish_metrics=True,
        target_cluster_size=0,
    )
    # Extract scale down duration and timestamp from the monitoring metrics collected above
    _, scale_down_time = _get_scaling_time(ec2_capacity_time_series_down, timestamps, 0, scale_down_start_timestamp)
    # Summarize the scaling metrics in a report (logs and metrics image)
    scaling_results = {
        "Region": region,
        "OS": os,
        "ComputeNode": instance,
        "HeadNode": head_node_instance_type,
        "ScalingTarget": scaling_target,
        "ScalingStrategy": scaling_strategy,
        "ScaleUpTimeEC2": scale_up_time_ec2,
        "ScaleUpTimeScheduler": scale_up_time_scheduler,
        "ScaleDownTime": scale_down_time,
    }
    logging.info(f"Scaling Results: {scaling_results}")
    produce_benchmark_metrics_report(
        title=", ".join("{0}[{1}] ".format(key, val) for (key, val) in scaling_results.items()),
        region=region,
        cluster_name=cluster.cfn_name,
        start_time=start_time,
        end_time=end_time,
        scaling_target=scaling_target,
        request=request,
        scaling_target_time=_datetime_to_minute(scaling_target_time),
    )

    # Verify that there was no EC2 over-scaling
    assert_that(max(ec2_capacity_time_series_up)).is_equal_to(scaling_target)
    # Verify that there was no Slurm nodes over-scaling
    assert_that(max(compute_nodes_time_series_up)).is_equal_to(scaling_target)
    # Verify all Slurm nodes were removed on scale down
    assert_that(compute_nodes_time_series_down[-1]).is_equal_to(0)

    with open(str(test_datadir / "results" / "baseline.json"), encoding="utf-8") as baseline_file:
        baseline_dict = json.loads(baseline_file.read())
    try:
        baseline_scale_up_time_ec2 = int(
            baseline_dict.get(instance).get(str(scaling_target)).get(scaling_strategy).get("scale_up_time_ec2")
        )
        baseline_scale_up_time_scheduler = int(
            baseline_dict.get(instance).get(str(scaling_target)).get(scaling_strategy).get("scale_up_time_scheduler")
        )
        baseline_scale_down_time = int(
            baseline_dict.get(instance).get(str(scaling_target)).get(scaling_strategy).get("scale_down_time")
        )

        # Verify scale up time for EC2
        assert_that(scale_up_time_ec2).is_less_than_or_equal_to(baseline_scale_up_time_ec2)
        # Verify scale up time for scheduler (EC2 + bootstrap)
        assert_that(scale_up_time_scheduler).is_less_than_or_equal_to(baseline_scale_up_time_scheduler)
        # Verify scale down time
        assert_that(scale_down_time).is_less_than_or_equal_to(baseline_scale_down_time)
    except AttributeError:
        logging.warning(
            f"Baseline for ComputeNode ({instance}), ScalingTarget ({scaling_target}), "
            f"ScalingStrategy ({scaling_strategy}) not found. "
            f"You need to build it in {str(test_datadir / 'results' / 'baseline.json')}"
        )
