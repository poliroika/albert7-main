# GNN Routing Example

Use graph neural networks for intelligent routing.

## Setup

```python
from gmas.core import AgentProfile, RoleGraph
from gmas.builder import build_property_graph
import torch
from torch_geometric.nn import GATConv
```

## Create Graph

```python
agents = [
    AgentProfile(agent_id=f"agent_{i}", display_name=f"Agent {i}")
    for i in range(5)
]

graph = build_property_graph(
    agents,
    workflow_edges=[
        ("agent_0", "agent_1"),
        ("agent_0", "agent_2"),
        ("agent_1", "agent_3"),
        ("agent_2", "agent_3"),
        ("agent_3", "agent_4"),
    ],
    query="Example query",
)
```

## Convert to PyG

```python
pyg_data = graph.to_pyg_data()
print(pyg_data)  # Data(x=[5, 768], edge_index=[2, 10])
```

## Train GNN

```python
class GNNRouter(torch.nn.Module):
    def __init__(self):
        super().__init__()
        self.conv1 = GATConv(768, 128, heads=4)
        self.conv2 = GATConv(512, 64, heads=1)

    def forward(self, data):
        x, edge_index = data.x, data.edge_index
        x = self.conv1(x, edge_index).relu()
        x = self.conv2(x, edge_index)
        return x

model = GNNRouter()
output = model(pyg_data)
```

## Update Graph

```python
# Use GNN output to update routing
graph.update_communication(
    A_com=new_adjacency,
    s_tilde=scores,
    p_matrix=probabilities,
)
```
