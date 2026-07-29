import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import ANY, MagicMock, patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "azure"))

import function  # noqa: E402


def resource(resource_id=None, **values):
    return SimpleNamespace(id=resource_id, **values)


class VirtualMachineOperations:
    def list_all(self):
        nic = resource("/subscriptions/sub/resourceGroups/network-rg/providers/Microsoft.Network/networkInterfaces/nic-1")
        vm = resource(
            "/subscriptions/sub/resourceGroups/vm-rg/providers/Microsoft.Compute/virtualMachines/legacy-vm",
            name="legacy-vm",
            location="japaneast",
            hardware_profile=resource(vm_size="Standard_B1s"),
            storage_profile=resource(os_disk=resource(os_type="Linux")),
            network_profile=resource(network_interfaces=[nic]),
        )
        return [vm]

    def instance_view(self, resource_group, vm_name):
        assert resource_group == "vm-rg"
        assert vm_name == "legacy-vm"
        return resource(statuses=[resource(code="PowerState/running", display_status="VM running")])


class NetworkInterfaceOperations:
    def get(self, resource_group, nic_name):
        assert resource_group == "network-rg"
        assert nic_name == "nic-1"
        public_ip = resource(
            "/subscriptions/sub/resourceGroups/network-rg/providers/Microsoft.Network/publicIPAddresses/ip-1"
        )
        return resource(ip_configurations=[resource(
            private_ip_address="10.0.0.4",
            public_ip_address=public_ip,
        )])


class PublicIpOperations:
    def get(self, resource_group, ip_name):
        assert resource_group == "network-rg"
        assert ip_name == "ip-1"
        return resource(ip_address="203.0.113.10")


class VmListTests(unittest.TestCase):
    def test_vm_is_joined_to_its_actual_nic_and_ip(self):
        compute_client = resource(virtual_machines=VirtualMachineOperations())
        network_client = resource(
            network_interfaces=NetworkInterfaceOperations(),
            public_ip_addresses=PublicIpOperations(),
        )
        with patch.object(function, "ComputeManagementClient", return_value=compute_client):
            with patch.object(function, "NetworkManagementClient", return_value=network_client):
                vms = function.list_vms("sub", object())

        self.assertEqual(len(vms), 1)
        self.assertEqual(vms[0]["resource_group"], "vm-rg")
        self.assertEqual(vms[0]["name"], "legacy-vm")
        self.assertEqual(vms[0]["power_state"], "运行中")
        self.assertEqual(vms[0]["private_ips"], ["10.0.0.4"])
        self.assertEqual(vms[0]["public_ips"], ["203.0.113.10"])


class CredentialValidationTests(unittest.TestCase):
    @patch.object(function, "ClientSecretCredential")
    def test_credential_uses_modern_token_credential(self, credential_class):
        expected_credential = object()
        credential_class.return_value = expected_credential

        credential = function.create_credential_object(
            "tenant-id", "client-id", "client-secret"
        )

        self.assertIs(credential, expected_credential)
        credential_class.assert_called_once_with(
            tenant_id="tenant-id",
            client_id="client-id",
            client_secret="client-secret",
        )

    @patch.object(function, "ResourceManagementClient")
    def test_validation_performs_read_only_subscription_request(self, resource_client_class):
        resource_client_class.return_value.resource_groups.list.return_value = iter([])

        function.validate_credential("subscription-id", object())

        resource_client_class.assert_called_once_with(ANY, "subscription-id")
        resource_client_class.return_value.resource_groups.list.assert_called_once_with()


class LongRunningOperationTests(unittest.TestCase):
    @patch.object(function, "NetworkManagementClient")
    @patch.object(function, "ComputeManagementClient")
    def test_create_vm_uses_begin_create_or_update(self, compute_client_class,
                                                   network_client_class):
        network_client = network_client_class.return_value
        compute_client = compute_client_class.return_value
        resources = (
            (network_client.virtual_networks, "vnet-id"),
            (network_client.subnets, "subnet-id"),
            (network_client.public_ip_addresses, "public-ip-id"),
            (network_client.network_security_groups, "nsg-id"),
            (network_client.network_interfaces, "nic-id"),
        )
        for operations, resource_id in resources:
            operations.begin_create_or_update.return_value.result.return_value = resource(
                resource_id
            )
        compute_client.virtual_machines.begin_create_or_update.return_value.result.return_value = resource(
            "vm-id"
        )

        function.create_or_update_vm(
            "subscription-id",
            object(),
            "vm-name",
            "japaneast",
            "vm-user",
            "strong-password",
            "Standard_B1s",
            "Debian_12_X64",
            "",
            64,
        )

        for operations, _ in resources:
            operations.begin_create_or_update.assert_called_once()
            operations.create_or_update.assert_not_called()
        compute_client.virtual_machines.begin_create_or_update.assert_called_once()
        compute_client.virtual_machines.create_or_update.assert_not_called()

    @patch.object(function, "ResourceManagementClient")
    @patch.object(function, "ComputeManagementClient")
    def test_vm_lifecycle_uses_begin_methods(self, compute_client_class,
                                             resource_client_class):
        compute_operations = compute_client_class.return_value.virtual_machines
        resource_group_operations = resource_client_class.return_value.resource_groups

        function.start_vm("subscription-id", object(), "resource-group", "vm-name")
        function.stop_vm("subscription-id", object(), "resource-group", "vm-name")
        function.delete_vm("subscription-id", object(), "resource-group")

        compute_operations.begin_start.assert_called_once_with(
            "resource-group", "vm-name"
        )
        compute_operations.begin_start.return_value.wait.assert_called_once_with()
        compute_operations.begin_deallocate.assert_called_once_with(
            "resource-group", "vm-name"
        )
        compute_operations.begin_deallocate.return_value.wait.assert_called_once_with()
        resource_group_operations.begin_delete.assert_called_once_with("resource-group")
        resource_group_operations.begin_delete.return_value.result.assert_called_once_with()
        compute_operations.start.assert_not_called()
        compute_operations.deallocate.assert_not_called()
        resource_group_operations.delete.assert_not_called()

    @patch.object(function.uuid, "uuid4")
    @patch.object(function, "NetworkManagementClient")
    @patch.object(function, "ComputeManagementClient")
    def test_static_ip_change_uses_begin_methods(self, compute_client_class,
                                                 network_client_class, uuid4):
        uuid4.return_value.hex = "12345678abcdef"
        compute_client = compute_client_class.return_value
        network_client = network_client_class.return_value
        nic_reference = resource(
            "/subscriptions/sub/resourceGroups/network-rg/providers/"
            "Microsoft.Network/networkInterfaces/nic-1"
        )
        public_ip_reference = resource(
            "/subscriptions/sub/resourceGroups/network-rg/providers/"
            "Microsoft.Network/publicIPAddresses/ip-1"
        )
        ip_configuration = resource(public_ip_address=public_ip_reference)
        nic = resource(ip_configurations=[ip_configuration])
        public_ip = resource(
            location="japaneast",
            public_ip_allocation_method="Static",
            public_ip_address_version="IPV4",
            zones=[],
        )
        compute_client.virtual_machines.get.return_value = resource(
            network_profile=resource(network_interfaces=[nic_reference])
        )
        network_client.network_interfaces.get.return_value = nic
        network_client.public_ip_addresses.get.return_value = public_ip
        network_client.public_ip_addresses.begin_create_or_update.return_value.result.return_value = resource(
            "replacement-ip-id"
        )

        function.change_ip(
            "subscription-id", object(), "vm-resource-group", "vm-name"
        )

        network_client.public_ip_addresses.begin_create_or_update.assert_called_once_with(
            "network-rg",
            "ip-1-12345678",
            {
                "location": "japaneast",
                "sku": {"name": "Standard"},
                "public_ip_allocation_method": "Static",
                "public_ip_address_version": "IPV4",
            },
        )
        network_client.network_interfaces.begin_create_or_update.assert_called_once_with(
            "network-rg", "nic-1", nic
        )
        network_client.public_ip_addresses.begin_delete.assert_called_once_with(
            "network-rg", "ip-1"
        )
        network_client.public_ip_addresses.create_or_update.assert_not_called()
        network_client.network_interfaces.create_or_update.assert_not_called()
        network_client.public_ip_addresses.delete.assert_not_called()


if __name__ == "__main__":
    unittest.main()
