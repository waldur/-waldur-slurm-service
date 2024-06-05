"""Functions shared between agent modules."""

import argparse
from importlib.metadata import version
from pathlib import Path
from typing import Set

import yaml
from waldur_client import WaldurClient, WaldurClientException

from waldur_site_agent.backends import (
    BackendType,
    logger,
)
from waldur_site_agent.backends.backend import BaseBackend, UnknownBackend
from waldur_site_agent.backends.slurm_backend import utils as slurm_utils
from waldur_site_agent.backends.slurm_backend.backend import SlurmBackend
from waldur_site_agent.backends.structures import Resource

from . import AgentMode, Offering, WaldurAgentConfiguration


def init_configuration() -> WaldurAgentConfiguration:
    """Loads configuration from CLI and config file to the dataclass."""
    configuration = WaldurAgentConfiguration()
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--mode",
        "-m",
        help="Agent mode, choices: order_process, report "
        "and membership_sync; default is order_process",
        choices=["order_process", "report", "membership_sync"],
        default="order_process",
    )

    parser.add_argument(
        "--config-file",
        "-c",
        help="Path to the config file with provider settings;"
        "default is waldur-site-agent-config.yaml",
        dest="config_file_path",
        default="waldur-site-agent-config.yaml",
        required=False,
    )

    cli_args = parser.parse_args()

    config_file_path = cli_args.config_file_path
    agent_mode = cli_args.mode

    logger.info("Using %s as a config source", config_file_path)

    with Path(config_file_path).open(encoding="UTF-8") as stream:
        config = yaml.safe_load(stream)
        offering_list = config["offerings"]
        waldur_offerings = [
            Offering(
                name=offering_info["name"],
                api_url=offering_info["waldur_api_url"],
                api_token=offering_info["waldur_api_token"],
                uuid=offering_info["waldur_offering_uuid"],
                backend_type=offering_info["backend_type"].lower(),
                backend_settings=offering_info["backend_settings"],
                backend_components=offering_info["backend_components"],
            )
            for offering_info in offering_list
        ]
        configuration.waldur_offerings = waldur_offerings

        sentry_dsn = config.get("sentry_dsn")
        if sentry_dsn:
            import sentry_sdk

            sentry_sdk.init(
                dsn=sentry_dsn,
            )
            configuration.sentry_dsn = sentry_dsn

    waldur_site_agent_version = version("waldur-site-agent")

    user_agent_dict = {
        AgentMode.ORDER_PROCESS.value: "waldur-site-agent-order-process/"
        + waldur_site_agent_version,
        AgentMode.REPORT.value: "waldur-site-agent-report/" + waldur_site_agent_version,
        AgentMode.MEMBERSHIP_SYNC.value: "waldur-site-agent-membership-sync/"
        + waldur_site_agent_version,
    }

    configuration.waldur_user_agent = user_agent_dict.get(agent_mode, "")
    configuration.waldur_site_agent_mode = agent_mode
    configuration.waldur_site_agent_version = waldur_site_agent_version

    return configuration


def get_backend_for_offering(offering: Offering) -> BaseBackend:
    """Creates a corresponding backend for an offering."""
    resource_backend: BaseBackend = UnknownBackend()
    if offering.backend_type == BackendType.SLURM.value:
        resource_backend = SlurmBackend(offering.backend_settings, offering.backend_components)
    elif offering.backend_type in {
        BackendType.MOAB.value,
        BackendType.CUSTOM.value,
    }:
        return resource_backend
    else:
        logger.error("Unknown backend type: %s", offering.backend_type)
        return UnknownBackend()

    return resource_backend


def delete_associations_from_waldur_allocation(
    waldur_rest_client: WaldurClient,
    backend_resource: Resource,
    usernames: Set[str],
) -> None:
    """Deletes a SLURM association for the specified resource and username in Waldur."""
    logger.info("Stale usernames: %s", " ,".join(usernames))
    for username in usernames:
        try:
            waldur_rest_client.delete_slurm_association(backend_resource.marketplace_uuid, username)
            logger.info(
                "The user %s has been dropped from %s (backend_id: %s)",
                username,
                backend_resource.name,
                backend_resource.backend_id,
            )
        except WaldurClientException as e:
            logger.error("User %s can not be dropped due to: %s", username, e)


def create_associations_for_waldur_allocation(
    waldur_rest_client: WaldurClient,
    backend_resource: Resource,
    usernames: Set[str],
) -> None:
    """Creates a SLURM association for the specified resource and username in Waldur."""
    logger.info("New usernames to add to Waldur allocation: %s", " ,".join(usernames))
    for username in usernames:
        try:
            waldur_rest_client.create_slurm_association(backend_resource.marketplace_uuid, username)
            logger.info(
                "The user %s has been added to %s (backend_id: %s)",
                username,
                backend_resource.name,
                backend_resource.backend_id,
            )
        except WaldurClientException as e:
            logger.error("User %s can not be added due to: %s", username, e)


def create_offering_components() -> None:
    """Creates offering components in Waldur based on data from the config file."""
    configuration = init_configuration()
    for offering in configuration.waldur_offerings:
        logger.info("Processing %s offering", offering.name)
        waldur_rest_client = WaldurClient(
            offering.api_url, offering.api_token, configuration.waldur_user_agent
        )

        if offering.backend_type == BackendType.SLURM.value:
            slurm_utils.create_offering_components(
                waldur_rest_client, offering.uuid, offering.name, offering.backend_components
            )


def diagnostics() -> bool:
    """Performs system check for offerings."""
    configuration = init_configuration()
    logger.info("-" * 10 + "DIAGNOSTICS START" + "-" * 10)
    logger.info("Provided settings:")
    format_string = "{:<30} = {:<10}"

    if AgentMode.ORDER_PROCESS.value == configuration.waldur_site_agent_mode:
        logger.info(
            "Agent is running in %s mode - "
            "pulling orders from Waldur and creating resources in backend",
            AgentMode.ORDER_PROCESS.name,
        )
    if AgentMode.REPORT.value == configuration.waldur_site_agent_mode:
        logger.info(
            "Agent is running in %s mode - pushing usage data to Waldur",
            AgentMode.REPORT.name,
        )
    if AgentMode.MEMBERSHIP_SYNC.value == configuration.waldur_site_agent_mode:
        logger.info(
            "Agent is running in %s mode - pushing membership data to Waldur",
            AgentMode.MEMBERSHIP_SYNC.name,
        )

    for offering in configuration.waldur_offerings:
        format_string = "{:<30} = {:<10}"
        offering_uuid = offering.uuid
        offering_name = offering.name
        offering_api_url = offering.api_url
        offering_api_token = offering.api_token

        logger.info(format_string.format("Offering name", offering_name))
        logger.info(format_string.format("Offering UUID", offering_uuid))
        logger.info(format_string.format("Waldur API URL", offering_api_url))
        logger.info(format_string.format("SENTRY_DSN", str(configuration.sentry_dsn)))

        waldur_rest_client = WaldurClient(
            offering_api_url, offering_api_token, configuration.waldur_user_agent
        )

        try:
            offering_data = waldur_rest_client.get_marketplace_provider_offering(offering_uuid)
            logger.info("Offering uuid: %s", offering_data["uuid"])
            logger.info("Offering name: %s", offering_data["name"])
            logger.info("Offering org: %s", offering_data["customer_name"])
            logger.info("Offering state: %s", offering_data["state"])

            logger.info("Offering components:")
            format_string = "{:<10} {:<10} {:<10} {:<10}"
            headers = ["Type", "Name", "Unit", "Limit"]
            logger.info(format_string.format(*headers))
            components = [
                [
                    component["type"],
                    component["name"],
                    component["measured_unit"],
                    component["limit_amount"],
                ]
                for component in offering_data["components"]
            ]
            for component in components:
                logger.info(format_string.format(*component))

            logger.info("")
        except WaldurClientException as err:
            logger.error("Unable to fetch offering data, reason: %s", err)

        logger.info("")
        try:
            orders = waldur_rest_client.list_orders(
                {
                    "offering_uuid": offering_uuid,
                    "state": ["pending-provider", "executing"],
                }
            )
            logger.info("Active orders:")
            format_string = "{:<10} {:<10} {:<10}"
            headers = ["Project", "Type", "State"]
            logger.info(format_string.format(*headers))
            for order in orders:
                logger.info(
                    format_string.format(order["project_name"], order["type"], order["state"])
                )
        except WaldurClientException as err:
            logger.error("Unable to fetch orders, reason: %s", err)

        backend_diagnostics_result = False
        if offering.backend_type == BackendType.SLURM.value:
            backend = SlurmBackend(offering.backend_settings, offering.backend_components)
            backend_diagnostics_result = slurm_utils.diagnostics(backend)

        if not backend_diagnostics_result:
            return False

    logger.info("-" * 10 + "DIAGNOSTICS END" + "-" * 10)
    return True


def create_homedirs_for_offering_users() -> None:
    """Creates homedirs for offering users in SLURM cluster."""
    configuration = init_configuration()
    for offering in configuration.waldur_offerings:
        # Feature is exclusive for SLURM temporarily
        if offering.backend_type != BackendType.SLURM.value or not offering.backend_settings.get(
            "enable_user_homedir_account_creation", True
        ):
            continue

        logger.info("Creating homedirs for %s offering users", offering.name)

        waldur_rest_client = WaldurClient(
            offering.api_url, offering.api_token, configuration.waldur_user_agent
        )

        offering_users = waldur_rest_client.list_remote_offering_users(
            {
                "offering_uuid": offering.uuid,
            }
        )

        offering_user_usernames: Set[str] = {
            offering_user["username"] for offering_user in offering_users
        }
        slurm_backend = SlurmBackend(offering.backend_settings, offering.backend_components)
        slurm_backend._create_user_homedirs(offering_user_usernames)
