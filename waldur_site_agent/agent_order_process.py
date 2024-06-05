"""Module for order processing."""

from __future__ import annotations

import traceback
from time import sleep
from typing import TYPE_CHECKING, Dict, List, Set

from waldur_client import (
    SlurmAllocationState,
    WaldurClientException,
    is_uuid,
)

from waldur_site_agent.backends import BackendType, logger
from waldur_site_agent.processors import OfferingBaseProcessor

if TYPE_CHECKING:
    from waldur_site_agent.backends.structures import Resource

from waldur_site_agent.backends.exceptions import BackendError

from . import Offering, WaldurAgentConfiguration, common_utils


class OfferingOrderProcessor(OfferingBaseProcessor):
    """Class for an offering processing.

    Processes related orders and creates necessary associations.
    """

    def process_offering(self) -> None:
        """Pulls data form Mastermind using REST client and creates objects on the backend."""
        logger.info(
            "Processing offering %s (%s)",
            self.offering.name,
            self.offering.uuid,
        )

        orders = self.waldur_rest_client.list_orders(
            {
                "offering_uuid": self.offering.uuid,
                "state": ["pending-provider", "executing"],
            }
        )

        if len(orders) == 0:
            logger.info("There are no pending or executing orders")
            return

        for order in orders:
            try:
                logger.info(
                    "Processing order %s (%s) with state %s",
                    order["attributes"].get("name", "N/A"),
                    order["uuid"],
                    order["state"],
                )

                if order["state"] == "executing":
                    logger.info("Order is executing already, no need for approval")
                else:
                    logger.info("Approving the order")
                    self.waldur_rest_client.marketplace_order_approve_by_provider(order["uuid"])
                    logger.info("Refreshing the order")
                    order = self.waldur_rest_client.get_order(order["uuid"])

                if order["type"] == "Create":
                    self._process_create_order(order)

                if order["type"] == "Update":
                    self._process_update_order(order)

                if order["type"] == "Terminate":
                    self._process_terminate_order(order)

                logger.info("Marking order as done")
                self.waldur_rest_client.marketplace_order_set_state_done(order["uuid"])

                logger.info("The order has been successfully processed")
            except WaldurClientException as e:
                logger.exception(
                    "Waldur REST client error while processing order %s: %s",
                    order["uuid"],
                    e,
                )
            except BackendError as e:
                logger.exception(
                    "Waldur SLURM client error while processing order %s: %s",
                    order["uuid"],
                    e,
                )
                self.waldur_rest_client.marketplace_order_set_state_erred(
                    order["uuid"],
                    error_message=str(e),
                    error_traceback=traceback.format_exc(),
                )

    def _create_resource(
        self,
        waldur_resource: Dict,
    ) -> Resource | None:
        resource_uuid = waldur_resource["uuid"]
        resource_name = waldur_resource["name"]

        logger.info("Creating resource %s", resource_name)

        if not is_uuid(resource_uuid):
            logger.error("Unexpected resource UUID format, skipping the order")
            return None

        # TODO: figure out how to generalize it
        if (
            waldur_resource["state"] != "Creating"
            and self.offering.backend_type == BackendType.SLURM.value
        ):
            logger.info(
                "Setting SLURM allocation state (%s) to CREATING (current state is %s)",
                waldur_resource["uuid"],
                waldur_resource["state"],
            )
            self.waldur_rest_client.set_slurm_allocation_state(
                resource_uuid, SlurmAllocationState.CREATING
            )

        backend_resource = self.resource_backend.create_resource(waldur_resource)
        if backend_resource.backend_id == "":
            msg = f"Unable to create a backend resource for offering {self.offering}"
            raise BackendError(msg)

        logger.info("Updating resource metadata in Waldur")
        self.waldur_rest_client.marketplace_resource_set_backend_id(
            resource_uuid, backend_resource.backend_id
        )

        if self.resource_backend.backend_type == BackendType.SLURM.value:
            logger.info("Setting SLURM allocation backend ID")
            self.waldur_rest_client.set_slurm_allocation_backend_id(
                waldur_resource["uuid"], backend_resource.backend_id
            )

            logger.info("Updating allocation limits in Waldur")
            self.waldur_rest_client.set_slurm_allocation_limits(
                waldur_resource["uuid"], backend_resource.limits
            )

        return backend_resource

    def _add_users_to_resource(
        self,
        backend_resource: Resource,
    ) -> None:
        logger.info("Adding users to resource")
        logger.info("Fetching Waldur resource team")
        team = self.waldur_rest_client.marketplace_resource_get_team(
            backend_resource.marketplace_uuid
        )
        user_uuids = {user["uuid"] for user in team}

        logger.info("Fetching Waldur offering users")
        offering_users_all = self.waldur_rest_client.list_remote_offering_users(
            {"offering_uuid": self.offering.uuid}
        )
        offering_usernames: Set[str] = {
            offering_user["username"]
            for offering_user in offering_users_all
            if offering_user["user_uuid"] in user_uuids
        }

        logger.info("Adding usernames to resource in backend")
        added_users = self.resource_backend.add_users_to_resource(
            backend_resource.backend_id, offering_usernames
        )

        common_utils.create_associations_for_waldur_allocation(
            self.waldur_rest_client, backend_resource, added_users
        )

    def _process_create_order(self, order: Dict) -> None:
        # Wait until resource is created
        attempts = 0
        max_attempts = 4
        while "marketplace_resource_uuid" not in order:
            if attempts > max_attempts:
                logger.error("Order processing timed out")
                return

            if order["state"] != "executing":
                logger.error("order has unexpected state %s", order["state"])
                return

            logger.info("Waiting for resource creation...")
            sleep(5)

            order = self.waldur_rest_client.get_order(order["uuid"])
            attempts += 1

        # TODO: drop this cycle...
        # TODO: after removal of waldur_slurm.Allocation model from Mastermind
        attempts = 0
        while order["resource_uuid"] is None:
            if attempts > max_attempts:
                logger.error("Order processing timed out")
                return

            if order["state"] != "executing":
                logger.error("order has unexpected state %s", order["state"])
                return

            logger.info("Waiting for Waldur allocation creation...")
            sleep(5)

            order = self.waldur_rest_client.get_order(order["uuid"])
            attempts += 1

        waldur_resource = self.waldur_rest_client.get_marketplace_resource(
            order["marketplace_resource_uuid"]
        )
        backend_resource = self._create_resource(waldur_resource)
        if backend_resource is None:
            msg = "Unable to create a resource"
            raise BackendError(msg)

        if self.offering.backend_type == BackendType.SLURM.value:
            logger.info("Updating Waldur resource scope state")
            self.waldur_rest_client.set_slurm_allocation_state(
                waldur_resource["uuid"], SlurmAllocationState.OK
            )

            self._add_users_to_resource(
                backend_resource,
            )

    def _process_update_order(self, order: dict) -> None:
        logger.info("Updating limits for %s", order["resource_name"])
        resource_uuid = order["marketplace_resource_uuid"]
        waldur_resource = self.waldur_rest_client.get_marketplace_resource(resource_uuid)

        if self.offering.backend_type == BackendType.SLURM.value:
            self.waldur_rest_client.set_slurm_allocation_state(
                resource_uuid, SlurmAllocationState.UPDATING
            )

        resource_backend = common_utils.get_backend_for_offering(self.offering)
        if resource_backend is None:
            return

        waldur_resource_backend_id = waldur_resource["backend_id"]

        new_limits = order["limits"]
        if not new_limits:
            logger.error(
                "Order %s (resource %s) with type" + "Update does not include new limits",
                order["uuid"],
                waldur_resource["name"],
            )

        resource_backend.set_resource_limits(waldur_resource_backend_id, new_limits)

        if self.offering.backend_type == BackendType.SLURM.value:
            logger.info("Updating Waldur resource scope state")
            self.waldur_rest_client.set_slurm_allocation_state(
                resource_uuid, SlurmAllocationState.OK
            )

        logger.info(
            "The limits for %s were updated successfully from %s to %s",
            waldur_resource["name"],
            order["attributes"]["old_limits"],
            new_limits,
        )

    def _process_terminate_order(self, order: dict) -> None:
        logger.info("Terminating resource %s", order["resource_name"])
        resource_uuid = order["marketplace_resource_uuid"]

        waldur_resource = self.waldur_rest_client.get_marketplace_resource(resource_uuid)

        resource_backend = common_utils.get_backend_for_offering(self.offering)
        if resource_backend is None:
            return

        resource_backend.delete_resource(
            waldur_resource["backend_id"], project_uuid=order["project_uuid"]
        )

        logger.info("Allocation has been terminated successfully")


def process_offerings(waldur_offerings: List[Offering], user_agent: str = "") -> None:
    """Processes offerings one-by-one."""
    logger.info("Number of offerings to process: %s", len(waldur_offerings))
    for offering in waldur_offerings:
        try:
            processor = OfferingOrderProcessor(offering, user_agent)
            processor.process_offering()
        except Exception as e:
            logger.exception("The application crashed due to the error: %s", e)


def start(configuration: WaldurAgentConfiguration) -> None:
    """Starts the main loop for offering processing."""
    logger.info("Synching data from Waldur")
    while True:
        try:
            process_offerings(configuration.waldur_offerings, configuration.waldur_user_agent)
        except Exception as e:
            logger.exception("The application crashed due to the error: %s", e)
        sleep(2 * 60)  # Once per 2 minutes
