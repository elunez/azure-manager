import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import ANY, patch


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


if __name__ == "__main__":
    unittest.main()
