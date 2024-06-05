"""Agent responsible for membership control."""

from time import sleep
from typing import Dict, List, Set

from waldur_client import (
    WaldurClientException,
)

from waldur_site_agent import common_utils
from waldur_site_agent.backends import BackendType, logger
from waldur_site_agent.backends.exceptions import BackendError
from waldur_site_agent.backends.structures import Resource
from waldur_site_agent.processors import OfferingBaseProcessor

from . import Offering, WaldurAgentConfiguration


class OfferingMembershipProcessor(OfferingBaseProcessor):
    """Class for an offering processing.

    Processes related resources and reports membership data to Waldur.
    """

    def process_offering(self) -> None:
        """Processes offering and reports resources usage to Waldur."""
        logger.info(
            "Processing offering %s (%s)",
            self.offering.name,
            self.offering.uuid,
        )

        waldur_resources = self.waldur_rest_client.filter_marketplace_resources(
            {
                "offering_uuid": self.offering.uuid,
                "state": "OK",
                "field": ["backend_id", "uuid", "name", "resource_uuid"],
            }
        )

        waldur_resources_info = [
            Resource(
                name=resource_data["name"],
                backend_id=resource_data["backend_id"],
                marketplace_uuid=resource_data["uuid"],
                backend_type=self.offering.backend_type,
                marketplace_scope_uuid=resource_data["resource_uuid"],
            )
            for resource_data in waldur_resources
        ]

        resource_report = self.resource_backend.pull_resources(waldur_resources_info)

        self._process_resources(resource_report)

    def _sync_slurm_resource_users(
        self,
        backend_resource: Resource,
    ) -> None:
        """Syncs users for the resource between SLURM cluster and Waldur."""
        # This method is currently implemented for SLURM backend only
        logger.info("Syncing user list for resource %s", backend_resource.name)
        usernames = backend_resource.users

        # Source of truth - associations in a SLURM cluster
        # The service fetches associations from the cluster and pushes them to Waldur
        associations = self.waldur_rest_client.list_slurm_associations(
            {"allocation_uuid": backend_resource.marketplace_scope_uuid}
        )
        remote_usernames = {association["username"] for association in associations}
        local_usernames = set(usernames)

        stale_usernames: Set[str] = remote_usernames - local_usernames
        common_utils.delete_associations_from_waldur_allocation(
            self.waldur_rest_client, backend_resource, stale_usernames
        )

        new_usernames = local_usernames - remote_usernames
        common_utils.create_associations_for_waldur_allocation(
            self.waldur_rest_client, backend_resource, new_usernames
        )

        # Offering users sync
        # The service fetches offering users from Waldur and pushes them to the cluster
        logger.info("Fetching Waldur resource team")
        team = self.waldur_rest_client.marketplace_resource_get_team(
            backend_resource.marketplace_uuid
        )
        user_uuids = {user["uuid"] for user in team}

        logger.info("Creating associations for offering users")
        offering_users = self.waldur_rest_client.list_remote_offering_users(
            {
                "offering_uuid": self.offering.uuid,
            }
        )

        offering_user_usernames: Set[str] = {
            offering_user["username"]
            for offering_user in offering_users
            if offering_user["username"] not in local_usernames
            and offering_user["user_uuid"] in user_uuids
        }

        common_utils.create_associations_for_waldur_allocation(
            self.waldur_rest_client, backend_resource, offering_user_usernames
        )

        self.resource_backend.add_users_to_resource(
            backend_resource.backend_id, offering_user_usernames
        )

    def _process_resources(
        self,
        resource_report: Dict[str, Resource],
    ) -> None:
        """Processes usage report for the resource."""
        # Push data to Mastermind using REST client

        # TODO: this part is not generic yet, rather SLURM-specific
        for resource_backend_id, backend_resource in resource_report.items():
            logger.info("-" * 30)
            try:
                logger.info("Processing %s", resource_backend_id)
                # Sync users
                if backend_resource.backend_type == BackendType.SLURM.value:
                    self._sync_slurm_resource_users(backend_resource)
            except WaldurClientException as e:
                logger.exception(
                    "Waldur REST client error while processing allocation %s: %s",
                    resource_backend_id,
                    e,
                )
            except BackendError as e:
                logger.exception(
                    "Waldur SLURM client error while processing allocation %s: %s",
                    resource_backend_id,
                    e,
                )


def process_offerings(waldur_offerings: List[Offering], user_agent: str = "") -> None:
    """Processes list of offerings."""
    logger.info("Number of offerings to process: %s", len(waldur_offerings))
    for offering in waldur_offerings:
        try:
            processor = OfferingMembershipProcessor(offering, user_agent)
            processor.process_offering()
        except Exception as e:
            logger.exception("The application crashed due to the error: %s", e)


def start(configuration: WaldurAgentConfiguration) -> None:
    """Starts the main loop for offering processing."""
    logger.info("Synching data to Waldur")
    while True:
        try:
            process_offerings(configuration.waldur_offerings, configuration.waldur_user_agent)
        except Exception as e:
            logger.exception("The application crashed due to the error: %s", e)
        sleep(60 * 60)  # Once per hour
