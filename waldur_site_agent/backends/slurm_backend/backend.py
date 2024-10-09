"""SLURM-specific backend classes and functions."""

from __future__ import annotations

from typing import Dict, List, Set, Tuple

from waldur_site_agent.backends import (
    BackendType,
    backend,
    logger,
)
from waldur_site_agent.backends import utils as backend_utils
from waldur_site_agent.backends.exceptions import BackendError

from . import utils
from .client import SlurmClient


class SlurmBackend(backend.BaseBackend):
    """Main class for management of SLURM resources."""

    def __init__(self, slurm_settings: Dict, slurm_tres: Dict[str, Dict]) -> None:
        """Init backend data and creates a corresponding client."""
        super().__init__(slurm_settings, slurm_tres)
        self.backend_type = BackendType.SLURM.value
        self.client: SlurmClient = SlurmClient(slurm_tres)

    def ping(self, raise_exception: bool = False) -> bool:
        """Check if the SLURM cluster is online."""
        try:
            self.client.list_accounts()
        except BackendError as err:
            if raise_exception:
                raise
            logger.info("Error: %s", err)
            return False
        else:
            return True

    def list_components(self) -> List[str]:
        """Return a list of TRES on the SLURM cluster."""
        return self.client.list_tres()

    def _collect_limits(
        self, waldur_resource: Dict[str, Dict]
    ) -> Tuple[Dict[str, int], Dict[str, int]]:
        """Collect SLURM and Waldur limits separately."""
        allocation_limits = backend_utils.get_usage_based_limits(self.backend_components)
        limit_based_components = [
            component
            for component, data in self.backend_components.items()
            if data["accounting_type"] == "limit"
        ]

        # Add limit-based limits
        for component_key in limit_based_components:
            allocation_limits[component_key] = (
                waldur_resource["limits"][component_key]
                * self.backend_components[component_key]["unit_factor"]
            )

        # Keep only limit-based components for Waldur resource
        waldur_resource_limits = {
            component_key: waldur_resource["limits"][component_key]
            for component_key, data in self.backend_components.items()
            if data["accounting_type"] == "limit"
        }

        return allocation_limits, waldur_resource_limits

    def add_users_to_resource(
        self, resource_backend_id: str, user_ids: Set[str], **kwargs: dict
    ) -> Set[str]:
        """Add specified users to the allocations on the SLURM cluster."""
        added_users = super().add_users_to_resource(resource_backend_id, user_ids)

        if self.backend_settings.get("enable_user_homedir_account_creation", True):
            umask: str = str(kwargs.get("homedir_umask", "0700"))
            self._create_user_homedirs(added_users, umask=umask)

        return added_users

    def downscale_resource(self, account: str) -> bool:
        """Downscale the resource QoS respecting the backend settings."""
        qos_downscaled = self.backend_settings.get("qos_downscaled")
        if not qos_downscaled:
            logger.error(
                "The QoS for dowscaling has incorrect value %s, skipping operation",
                qos_downscaled,
            )
            return False

        logger.info("Setting %s QoS for the SLURM account", qos_downscaled)
        self.client.set_account_qos(account, qos_downscaled)
        return True

    def _create_user_homedirs(self, usernames: Set[str], umask: str = "0700") -> None:
        logger.info("Creating homedirs for users")
        for username in usernames:
            try:
                self.client.create_linux_user_homedir(username, umask)
                logger.info("Homedir for user %s has been created", username)
            except BackendError as err:
                logger.exception(
                    "Unable to create user homedir for %s, reason: %s",
                    username,
                    err,
                )

    def _get_usage_report(self, accounts: List[str]) -> Dict[str, Dict[str, Dict[str, int]]]:
        """Example output.

        {
            "account_name": {
                "TOTAL_ACCOUNT_USAGE": {
                    'cpu': 1,
                    'gres/gpu': 2,
                    'mem': 3,
                },
                "user1": {
                    'cpu': 1,
                    'gres/gpu': 2,
                    'mem': 3,
                },
            }
        }
        """
        report: Dict[str, Dict[str, Dict[str, int]]] = {}
        lines = self.client.get_usage_report(accounts)

        for line in lines:
            report.setdefault(line.account, {}).setdefault(line.user, {})
            tres_usage = line.tres_usage
            user_usage_existing = report[line.account][line.user]
            user_usage_new = backend_utils.sum_dicts([user_usage_existing, tres_usage])
            report[line.account][line.user] = user_usage_new

        for account_usage in report.values():
            usages_per_user = list(account_usage.values())
            total = backend_utils.sum_dicts(usages_per_user)
            account_usage["TOTAL_ACCOUNT_USAGE"] = total

        # Convert SLURM units to Waldur ones
        report_converted: Dict[str, Dict[str, Dict[str, int]]] = {}
        for account, account_usage in report.items():
            report_converted[account] = {}
            for username, usage_dict in account_usage.items():
                converted_usage_dict = utils.convert_slurm_units_to_waldur_ones(
                    self.backend_components, usage_dict
                )
                report_converted[account][username] = converted_usage_dict

        return report_converted

    def _get_allocation_limits(self, account: str) -> Dict[str, int]:
        """Return limits converted to Waldur-readable values."""
        lines = self.client.get_resource_limits(account)
        correct_lines = [
            association.tres_limits for association in lines if association.tres_limits
        ]
        if len(correct_lines) == 0:
            return {}

        return utils.convert_slurm_units_to_waldur_ones(
            self.backend_components, correct_lines[0], to_int=True
        )

    def set_resource_limits(self, resource_backend_id: str, limits: Dict[str, int]) -> None:
        """Set limits for limit-based components in the SLURM allocation."""
        # Convert limits
        converted_limits = {
            key: value * self.backend_components[key]["unit_factor"]
            for key, value in limits.items()
        }
        super().set_resource_limits(resource_backend_id, converted_limits)
