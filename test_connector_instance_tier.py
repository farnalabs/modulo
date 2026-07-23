from modulo.db.crud.connector_instance import list_connector_instances
from modulo.db.models.connector_instance import ConnectorInstance

from conftest import TierFilterTestBase


class TestConnectorInstanceTierFilter(TierFilterTestBase):
    crud_func = list_connector_instances
    model_class = ConnectorInstance
