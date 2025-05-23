#  Copyright 2021 Amazon.com, Inc. or its affiliates. All Rights Reserved.
#
#  Licensed under the Apache License, Version 2.0 (the "License"). You may not use this file except in compliance
#  with the License. A copy of the License is located at http://aws.amazon.com/apache2.0/
#  or in the "LICENSE.txt" file accompanying this file. This file is distributed on an "AS IS" BASIS, WITHOUT WARRANTIES
#  OR CONDITIONS OF ANY KIND, express or implied. See the License for the specific language governing permissions and
#  limitations under the License.
# pylint: disable=import-outside-toplevel
import json
import textwrap
from functools import partial
from typing import List
from pcluster.models.cluster import Cluster

import argparse

import boto3
import requests

from pcluster import utils
from pcluster.cli.commands.common import CliCommand, print_json, to_bool

class SlurmApiCommand:
    def __init__(self, name: str, method: str, path: str, help: str, body_var: str = None, options: list=()):
        self.name = name
        self.method = method
        self.path = path
        self.help = help
        self.body_var = body_var
        self.options = options

    def get_path(self, args: dict = None):
        return self.path.format(**args) if args else self.path

class SlurmApiCommandArg:
    def __init__(self, name: str, flag: str, short_flag: str, arg_type: type, required: bool):
        self.name = name
        self.flag = flag
        self.short_flag = short_flag
        self.arg_type = arg_type
        self.required = required

API_COMMANDS = [
    SlurmApiCommand('ping', 'get', 'ping', 'Ping test'),
    SlurmApiCommand('partitions', 'get', 'partitions', 'Partitions info'),
    SlurmApiCommand('nodes', 'get', 'nodes', 'Nodes info'),
    SlurmApiCommand('diag', 'get', 'diag', 'Get diagnostics'),
    SlurmApiCommand('list-jobs', 'get', 'jobs', 'List active jobs'),
    SlurmApiCommand('describe-job', 'get', 'job/{job_id}', 'Describe a job', options=[
        SlurmApiCommandArg('job_id', '--job-id', '-j', int, True),
    ]),
    SlurmApiCommand('cancel-job', 'delete', 'job/{job_id}', 'Cancel a job', options=[
        SlurmApiCommandArg('job_id', '--job-id', '-j', int, True),
    ]),
    SlurmApiCommand('submit-job', 'post', 'job/submit', 'Submit a job', body_var="job", options=[
        SlurmApiCommandArg('job', '--job', '-j', str, True),
    ]),
]

def get_jwt_token(cluster_name):
    client = boto3.client('secretsmanager')
    boto_response = client.get_secret_value(SecretId=f'slurm_token_{cluster_name}')
    return boto_response['SecretString']

def get_head_node_ip(cluster_name):
    try:
        head_node = Cluster(cluster_name).head_node_instance
        return head_node.public_ip or head_node.private_ip
    except Exception as e:
        utils.error(f"Unable to connect to the cluster {cluster_name}.\n{e}")

def call_slurm_api(args, extra_args):
    head_node_ip = get_head_node_ip(args.cluster_name)
    jwt_token = get_jwt_token(args.cluster_name)

    base_url = f'https://{head_node_ip}/slurm/v0.0.39'
    kwargs = {
        'headers': {'X-SLURM-USER-TOKEN': jwt_token},
        'verify': False
    }

    if args.cluster_user:
        kwargs['headers']['X-SLURM-USER-NAME'] = args.cluster_user

    if args.dryrun:
        return f"Command: curl -X POST {base_url}/job/submit -H 'X-SLURM-USER-TOKEN: {jwt_token}' -d @job.json"

    api_command = next((command for command in API_COMMANDS if command.name == args.command), None)
    if api_command is None:
        utils.error(f"Unknown command: {args.command}")
    method = api_command.method
    url = f'{base_url}/{api_command.get_path(args.__dict__)}'

    if api_command.body_var and api_command.body_var in args.__dict__:
        json_file_path = args.__dict__[api_command.body_var]
        with open(json_file_path) as body_json_file:
            body_json = json.load(body_json_file)
        kwargs['json'] = body_json

    response = requests.request(method, url, **kwargs)

    return response.status_code, response.text

class SlurmApiCommand(CliCommand):
    """Implement pcluster slurm-api command."""

    # CLI
    name = "slurm-api"
    help = "Send SLURM API command."
    description = "Send SLURM API command."
    epilog = textwrap.dedent(
        """Example:

  pcluster slurm-api --cluster-name mycluster list-jobs
  pcluster slurm-api --cluster-name mycluster --cluster-user ec2-user list-jobs
        """
    )

    def __init__(self, subparsers):
        super().__init__(
            subparsers,
            name=self.name,
            help=self.help,
            description=self.description,
            epilog=self.epilog,
            formatter_class=argparse.RawDescriptionHelpFormatter,
            expects_extra_args=True,
        )

    def register_command_args(self, parser: argparse.ArgumentParser) -> None:  # noqa: D102
        parser.add_argument("-n", "--cluster-name", help="Name of the cluster to connect to.", required=True)
        parser.add_argument("-u", "--cluster-user", help="Name of the cluster user to submit the request as.", required=False)
        parser.add_argument(
            "--dryrun",
            default=False,
            type=partial(to_bool, "dryrun"),
            help="Prints curl command and exits (defaults to 'false').",
        )
        subparsers = parser.add_subparsers(dest='command', required=True)

        for api_command in API_COMMANDS:
            subparser = subparsers.add_parser(api_command.name, help=api_command.help)
            for api_command_opt in api_command.options:
                subparser.add_argument(
                    api_command_opt.short_flag,
                    api_command_opt.flag,
                    type=api_command_opt.arg_type,
                    required=api_command_opt.required,
                )

    def execute(  # noqa: D102
        self, args: argparse.Namespace, extra_args: List[str]  # pylint: disable=unused-argument
    ) -> None:
        status, body = call_slurm_api(args, extra_args)
        print_json({"status": status, "body": json.loads(body)})
