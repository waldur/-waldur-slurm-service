"""Module for abstract offering processor."""

from __future__ import annotations

import abc
import datetime
import traceback
from time import sleep
from typing import Dict, List, Optional, Set

from waldur_client import (
    ComponentUsage,
    SlurmAllocationState,
    WaldurClient,
    is_uuid,
)

from waldur_site_agent import Offering, common_utils
from waldur_site_agent.backends import BackendType, logger, utils
from waldur_site_agent.backends.exceptions import BackendError
from waldur_site_agent.backends.structures import Resource

from . import MARKETPLACE_SLURM_OFFERING_TYPE


class OfferingBaseProcessor(abc.ABC):
    """Abstract class for an offering processing."""

    def __init__(self, offering: Offering, user_agent: str = "") -> None:
        """Constructor."""
        self.offering: Offering = offering
        self.waldur_rest_client: WaldurClient = WaldurClient(
            offering.api_url, offering.api_token, user_agent
        )
        self.resource_backend = common_utils.get_backend_for_offering(offering)
        if self.resource_backend.backend_type == BackendType.UNKNOWN.value:
            raise BackendError(f"Unable to create backend for {self.offering}")

        self._print_current_user()

        waldur_offering = self.waldur_rest_client._get_offering(self.offering.uuid)
        common_utils.extend_backend_components(self.offering, waldur_offering["components"])

    def _print_current_user(self) -> None:
        current_user = self.waldur_rest_client.get_current_user()
        common_utils.print_current_user(current_user)

    @abc.abstractmethod
    def process_offering(self) -> None:
        """Pulls data form Mastermind using REST client and creates objects on the backend."""


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
            self.process_order(order)

    def get_order_info(self, order_uuid: str) -> Optional[dict]:
        """Get order info from Waldur."""
        try:
            return self.waldur_rest_client.get_order(order_uuid)
        except Exception as e:
            logger.error("Failed to get order %s info: %s", order_uuid, e)
            return None

    def process_order(self, order: dict) -> None:
        """Process a single order."""
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

            order_is_done = False

            if order["type"] == "Create":
                order_is_done = self._process_create_order(order)

            if order["type"] == "Update":
                order_is_done = self._process_update_order(order)

            if order["type"] == "Terminate":
                order_is_done = self._process_terminate_order(order)

            # TODO: no need for update of orders for marketplace SLURM offerings
            if order_is_done:
                logger.info("Marking order as done")
                self.waldur_rest_client.marketplace_order_set_state_done(order["uuid"])

                logger.info("The order has been successfully processed")
            else:
                logger.warning("The order processing was not finished, skipping to the next one")

        except Exception as e:
            logger.exception(
                "Error while processing order %s: %s",
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
            and waldur_resource["offering_type"] == MARKETPLACE_SLURM_OFFERING_TYPE
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
        self.waldur_rest_client.marketplace_provider_resource_set_backend_id(
            resource_uuid, backend_resource.backend_id
        )

        if waldur_resource["offering_type"] == MARKETPLACE_SLURM_OFFERING_TYPE:
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
        team = self.waldur_rest_client.marketplace_provider_resource_get_team(
            backend_resource.marketplace_uuid
        )
        user_uuids = {user["uuid"] for user in team}

        logger.info("Fetching Waldur offering users")
        offering_users_all = self.waldur_rest_client.list_remote_offering_users(
            {"offering_uuid": self.offering.uuid, "is_restricted": False}
        )
        offering_usernames: Set[str] = {
            offering_user["username"]
            for offering_user in offering_users_all
            if offering_user["user_uuid"] in user_uuids and offering_user["username"] != ""
        }

        logger.info("Adding usernames to resource in backend")
        self.resource_backend.add_users_to_resource(
            backend_resource.backend_id,
            offering_usernames,
            homedir_umask=self.offering.backend_settings.get("homedir_umask", "0700"),
        )

    def _process_create_order(self, order: Dict) -> bool:
        # Wait until resource is created
        attempts = 0
        max_attempts = 4
        while "marketplace_resource_uuid" not in order:
            if attempts > max_attempts:
                logger.error("Order processing timed out")
                return False

            if order["state"] != "executing":
                logger.error("order has unexpected state %s", order["state"])
                return False

            logger.info("Waiting for resource creation...")
            sleep(5)

            order = self.waldur_rest_client.get_order(order["uuid"])
            attempts += 1

        if order["offering_type"] == MARKETPLACE_SLURM_OFFERING_TYPE:
            # TODO: drop this cycle
            # after removal of waldur_slurm.Allocation model from Mastermind
            attempts = 0
            while order["resource_uuid"] is None:
                if attempts > max_attempts:
                    logger.error("Order processing timed out")
                    return False

                if order["state"] != "executing":
                    logger.error("order has unexpected state %s", order["state"])
                    return False

                logger.info("Waiting for Waldur allocation creation...")
                sleep(5)

                order = self.waldur_rest_client.get_order(order["uuid"])
                attempts += 1

        waldur_resource = self.waldur_rest_client.get_marketplace_provider_resource(
            order["marketplace_resource_uuid"]
        )

        waldur_resource["project_slug"] = order["project_slug"]
        waldur_resource["customer_slug"] = order["customer_slug"]

        backend_resource = self._create_resource(waldur_resource)
        if backend_resource is None:
            msg = "Unable to create a resource"
            raise BackendError(msg)

        if order["offering_type"] == MARKETPLACE_SLURM_OFFERING_TYPE:
            logger.info("Updating Waldur resource scope state")
            self.waldur_rest_client.set_slurm_allocation_state(
                waldur_resource["uuid"], SlurmAllocationState.OK
            )

            self._add_users_to_resource(
                backend_resource,
            )

        return True

    def _process_update_order(self, order: dict) -> bool:
        logger.info("Updating limits for %s", order["resource_name"])
        resource_uuid = order["marketplace_resource_uuid"]
        waldur_resource = self.waldur_rest_client.get_marketplace_provider_resource(resource_uuid)

        if order["offering_type"] == MARKETPLACE_SLURM_OFFERING_TYPE:
            self.waldur_rest_client.set_slurm_allocation_state(
                resource_uuid, SlurmAllocationState.UPDATING
            )

        resource_backend = common_utils.get_backend_for_offering(self.offering)
        if resource_backend is None:
            return False

        waldur_resource_backend_id = waldur_resource["backend_id"]

        new_limits = order["limits"]
        if not new_limits:
            logger.error(
                "Order %s (resource %s) with type" + "Update does not include new limits",
                order["uuid"],
                waldur_resource["name"],
            )

        if new_limits:
            resource_backend.set_resource_limits(waldur_resource_backend_id, new_limits)

        if order["offering_type"] == MARKETPLACE_SLURM_OFFERING_TYPE:
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
        return True

    def _process_terminate_order(self, order: dict) -> bool:
        logger.info("Terminating resource %s", order["resource_name"])
        resource_uuid = order["marketplace_resource_uuid"]

        waldur_resource = self.waldur_rest_client.get_marketplace_provider_resource(resource_uuid)
        project_slug = order["project_slug"]

        resource_backend = common_utils.get_backend_for_offering(self.offering)
        if resource_backend is None:
            return False

        resource_backend.delete_resource(waldur_resource["backend_id"], project_slug=project_slug)

        logger.info("Allocation has been terminated successfully")
        return True


class OfferingMembershipProcessor(OfferingBaseProcessor):
    """Class for an offering processing.

    Processes related resources and reports membership data to Waldur.
    """

    def _get_waldur_resources(self) -> List[Resource]:
        waldur_resources = self.waldur_rest_client.filter_marketplace_provider_resources(
            {
                "offering_uuid": self.offering.uuid,
                "state": "OK",
                "field": [
                    "backend_id",
                    "uuid",
                    "name",
                    "resource_uuid",
                    "offering_type",
                    "restrict_member_access",
                    "downscaled",
                    "paused",
                ],
            }
        )

        if len(waldur_resources) == 0:
            logger.info("No resources to process")
            return []

        return [
            Resource(
                name=resource_data["name"],
                backend_id=resource_data["backend_id"],
                marketplace_uuid=resource_data["uuid"],
                backend_type=self.offering.backend_type,
                marketplace_scope_uuid=resource_data["resource_uuid"],
                restrict_member_access=resource_data.get("restrict_member_access", False),
                downscaled=resource_data.get("downscaled", False),
                paused=resource_data.get("paused", False),
            )
            for resource_data in waldur_resources
        ]

    def process_offering(self) -> None:
        """Processes offering and reports resources usage to Waldur."""
        logger.info(
            "Processing offering %s (%s)",
            self.offering.name,
            self.offering.uuid,
        )

        waldur_resources_info = self._get_waldur_resources()

        resource_report = self.resource_backend.pull_resources(waldur_resources_info)

        self._process_resources(resource_report)

    def _get_user_offering_users(self, user_uuid: str) -> List[dict]:
        return self.waldur_rest_client.list_remote_offering_users(
            {
                "offering_uuid": self.offering.uuid,
                "user_uuid": user_uuid,
                "is_restricted": False,
            }
        )

    def process_user_role_changed(self, user_uuid: str, granted: bool) -> None:
        """Process event of user role changing."""
        offering_users = self._get_user_offering_users(user_uuid)
        if len(offering_users) == 0:
            logger.info(
                "User %s is not linked to the offering %s (%s)",
                user_uuid,
                self.offering.name,
                self.offering.uuid,
            )
            return

        username = offering_users[0]["username"]
        logger.info("Using offering user with username %s", username)
        if not username:
            logger.warning("Username is blank, skipping processing")
            return

        resources = self._get_waldur_resources()
        resource_report = self.resource_backend.pull_resources(resources)

        for resource in resource_report.values():
            try:
                if granted:
                    if resource.restrict_member_access:
                        logger.info("The resource is restricted, skipping new role.")
                        continue
                    self.resource_backend.add_user(resource.backend_id, username)
                else:
                    self.resource_backend.remove_user(resource.backend_id, username)
            except Exception as exc:
                logger.error(
                    "Unable to add user %s to the resource %s, error: %s",
                    username,
                    resource.backend_id,
                    exc,
                )

    def _sync_slurm_resource_users(
        self,
        resource: Resource,
    ) -> None:
        """Syncs users for the resource between SLURM cluster and Waldur."""
        # This method is currently implemented for SLURM backend only
        logger.info("Syncing user list for resource %s", resource.name)
        usernames = resource.users
        local_usernames = set(usernames)
        logger.info("The usernames from the backend: %s", ", ".join(local_usernames))

        # Offering users sync
        # The service fetches offering users from Waldur and pushes them to the cluster
        # If an offering user is not in the team anymore, it will be removed from the backend
        logger.info("Synching offering users")
        team = self.waldur_rest_client.marketplace_provider_resource_get_team(
            resource.marketplace_uuid
        )
        team_user_uuids = {user["uuid"] for user in team}

        offering_users = self.waldur_rest_client.list_remote_offering_users(
            {
                "offering_uuid": self.offering.uuid,
                "is_restricted": False,
            }
        )

        if resource.restrict_member_access:
            # The idea is to remove the existing associations in both sides
            # and avoid creation of new associations
            logger.info("Resource restricted for members, removing all the existing associations")
            existing_offering_user_usernames: Set[str] = {
                offering_user["username"]
                for offering_user in offering_users
                if offering_user["username"] in local_usernames
                and offering_user["user_uuid"] in team_user_uuids
            }

            self.resource_backend.remove_users_from_account(
                resource.backend_id, existing_offering_user_usernames
            )
            return

        new_offering_user_usernames: Set[str] = {
            offering_user["username"]
            for offering_user in offering_users
            if offering_user["username"] not in local_usernames
            and offering_user["user_uuid"] in team_user_uuids
        }

        stale_offering_user_usernames: Set[str] = {
            offering_user["username"]
            for offering_user in offering_users
            if offering_user["username"] in local_usernames
            and offering_user["user_uuid"] not in team_user_uuids
        }

        self.resource_backend.add_users_to_resource(
            resource.backend_id,
            new_offering_user_usernames,
            homedir_umask=self.offering.backend_settings.get("homedir_umask", "0700"),
        )

        self.resource_backend.remove_users_from_account(
            resource.backend_id,
            stale_offering_user_usernames,
        )

    def _sync_resource(self, resource: Resource) -> None:
        if resource.paused:
            logger.info("The resource pausing is requested, processing it")
            pausing_done = self.resource_backend.pause_resource(resource.backend_id)
            if pausing_done:
                logger.info("The pausing is successfully completed")
            else:
                logger.warning("The pausing is not done")
        elif resource.downscaled:
            logger.info("The resource downscaling is requested, processing it")
            downscaling_done = self.resource_backend.downscale_resource(resource.backend_id)
            if downscaling_done:
                logger.info("The downscaling is successfully completed")
            else:
                logger.warning("The downscaling is not done")
        else:
            logger.info(
                "The resource is not downscaled or paused, " "resetting the QoS to the default one"
            )
            restoring_done = self.resource_backend.restore_resource(resource.backend_id)
            if restoring_done:
                logger.info("The restoring is successfully completed")
            else:
                logger.info("The restoring is skipped")

        resource_metadata = self.resource_backend.get_resource_metadata(resource.backend_id)
        self.waldur_rest_client.marketplace_provider_resource_set_backend_metadata(
            resource.marketplace_uuid, resource_metadata
        )

    def _process_resource(self, backend_resource: Resource) -> None:
        try:
            logger.info("Processing %s", backend_resource.backend_id)
            if self.offering.backend_type == "slurm":
                self._sync_slurm_resource_users(backend_resource)
            self._sync_resource(backend_resource)
        except Exception as e:
            logger.exception(
                "Error while processing allocation %s: %s",
                backend_resource.backend_id,
                e,
            )
            error_traceback = traceback.format_exc()
            common_utils.mark_waldur_resources_as_erred(
                self.waldur_rest_client,
                [backend_resource],
                error_details={
                    "error_message": str(e),
                    "error_traceback": error_traceback,
                },
            )

    def _process_resources(
        self,
        resource_report: Dict[str, Resource],
    ) -> None:
        """Sync membership data for the resource."""
        for backend_resource in resource_report.values():
            self._process_resource(backend_resource)


class OfferingReportProcessor(OfferingBaseProcessor):
    """Class for an offering processing.

    Processes related resource and reports computing data to Waldur.
    """

    def process_offering(self) -> None:
        """Processes offering and reports resources usage to Waldur."""
        logger.info(
            "Processing offering %s (%s)",
            self.offering.name,
            self.offering.uuid,
        )

        waldur_resources = self.waldur_rest_client.filter_marketplace_provider_resources(
            {
                "offering_uuid": self.offering.uuid,
                "state": ["OK", common_utils.RESOURCE_ERRED_STATE],
                "field": ["backend_id", "uuid", "name", "offering_type", "state"],
            }
        )

        if len(waldur_resources) == 0:
            logger.info("No resources to process")
            return

        offering_type = waldur_resources[0].get("offering_type", "")

        waldur_resources_info = [
            Resource(
                name=resource_data["name"],
                backend_id=resource_data["backend_id"],
                marketplace_uuid=resource_data["uuid"],
                backend_type=self.offering.backend_type,
                state=resource_data["state"],
            )
            for resource_data in waldur_resources
        ]

        resource_report = self.resource_backend.pull_resources(waldur_resources_info)

        # TODO: make generic
        if offering_type == MARKETPLACE_SLURM_OFFERING_TYPE:
            # Allocations existing in Waldur but missing in SLURM cluster
            missing_resources = [
                Resource(
                    marketplace_uuid=resource_info["uuid"],
                    backend_id=resource_info["backend_id"],
                )
                for resource_info in waldur_resources
                if resource_info["backend_id"] not in set(resource_report.keys())
                and resource_info["state"] != common_utils.RESOURCE_ERRED_STATE
            ]
            logger.info("Number of missing resources %s", len(missing_resources))
            if len(missing_resources) > 0:
                common_utils.mark_waldur_resources_as_erred(
                    self.waldur_rest_client,
                    missing_resources,
                    {"error_message": "The resource is missing on the backend"},
                )

        self._process_resources(resource_report)

    def _submit_total_usage_for_resource(
        self,
        backend_resource: Resource,
        total_usage: Dict[str, float],
        waldur_components: List[Dict],
    ) -> None:
        """Reports total usage for a backend resource to Waldur."""
        logger.info("Setting usages: %s", total_usage)
        resource_uuid = backend_resource.marketplace_uuid
        plan_periods = self.waldur_rest_client.marketplace_provider_resource_get_plan_periods(
            resource_uuid
        )

        if len(plan_periods) == 0:
            logger.warning(
                "A corresponding ResourcePlanPeriod for resource %s was not found",
                backend_resource.name,
            )
            return

        plan_period = plan_periods[0]
        component_types = [component["type"] for component in waldur_components]
        missing_components = set(total_usage) - set(component_types)

        if missing_components:
            logger.warning(
                "The following components are not found in Waldur: %s",
                ", ".join(missing_components),
            )

        usage_objects = [
            ComponentUsage(type=component, amount=amount)
            for component, amount in total_usage.items()
            if component in component_types
        ]
        self.waldur_rest_client.create_component_usages(plan_period["uuid"], usage_objects)

    def _submit_user_usage_for_resource(
        self,
        username: str,
        user_usage: Dict[str, float],
        waldur_component_usages: List[Dict],
    ) -> None:
        """Reports per-user usage for a backend resource to Waldur."""
        logger.info("Setting usages for %s", username)
        component_usage_types = [
            component_usage["type"] for component_usage in waldur_component_usages
        ]
        missing_components = set(user_usage) - set(component_usage_types)

        if missing_components:
            logger.warning(
                "The following components are not found in Waldur: %s",
                ", ".join(missing_components),
            )

        offering_users = self.waldur_rest_client.list_remote_offering_users(
            {"username": username, "query": self.offering.uuid}
        )
        offering_user_uuid = None

        if len(offering_users) > 0:
            offering_user_uuid = offering_users[0]["uuid"]

        for component_usage in waldur_component_usages:
            component_type = component_usage["type"]
            usage = user_usage[component_type]
            logger.info(
                "Submitting usage for username %s: %s -> %s",
                username,
                component_type,
                usage,
            )
            self.waldur_rest_client.create_component_user_usage(
                component_usage["uuid"], usage, username, offering_user_uuid
            )

    def _process_resources(
        self,
        resource_report: Dict[str, Resource],
    ) -> None:
        """Processes usage report for the resource."""
        waldur_offering = self.waldur_rest_client._get_offering(self.offering.uuid)
        month_start = utils.month_start(datetime.datetime.now()).date()

        # TODO: this part is not generic yet, rather SLURM-specific
        for resource_backend_id, backend_resource in resource_report.items():
            try:
                logger.info("Processing %s", resource_backend_id)
                usages: Dict[str, Dict[str, float]] = backend_resource.usage

                # Set resource state OK if it is erred
                if backend_resource.state == common_utils.RESOURCE_ERRED_STATE:
                    self.waldur_rest_client.marketplace_provider_resource_set_as_ok(
                        backend_resource.marketplace_uuid
                    )

                # Submit usage
                total_usage = usages.pop("TOTAL_ACCOUNT_USAGE")
                self._submit_total_usage_for_resource(
                    backend_resource,
                    total_usage,
                    waldur_offering["components"],
                )

                # Skip the following actions if the dict is empty
                if not usages:
                    continue

                waldur_component_usages = self.waldur_rest_client.list_component_usages(
                    backend_resource.marketplace_uuid, date_after=month_start
                )

                logger.info("Setting per-user usages")
                for username, user_usage in usages.items():
                    self._submit_user_usage_for_resource(
                        username, user_usage, waldur_component_usages
                    )
            except Exception as e:
                logger.exception(
                    "Waldur REST client error while processing allocation %s: %s",
                    resource_backend_id,
                    e,
                )
                error_traceback = traceback.format_exc()
                common_utils.mark_waldur_resources_as_erred(
                    self.waldur_rest_client,
                    [backend_resource],
                    error_details={
                        "error_message": str(e),
                        "error_traceback": error_traceback,
                    },
                )
