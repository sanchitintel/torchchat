from dataclasses import dataclass, field
from torch.distributed.device_mesh import init_device_mesh

@dataclass
class ParallelConfig:
    name: str = field(default="")
    fp8_linear: str = field(default="")
    tp_degree: int = field(default=1)
    pp_degree: int = field(default=1)


@dataclass
class ParallelDims:
    tp: int
    pp: int
    world_size: int

    def __post_init__(self):
        self._validate()

    def _validate(self):
        tp, pp = self.tp, self.pp
        assert tp >= 1, tp
        assert pp >= 1, pp
        assert (
            tp * pp == self.world_size
        ), f"Invalid parallel dims: tp({tp}) * pp({pp}) != WORLD_SIZE({self.world_size})"

    def build_mesh(self, device_type):
        dims = []
        names = []
        for d, name in zip(
            [self.pp, self.tp], ["pp", "tp"], strict=True
        ):
            if d > 1:
                dims.append(d)
                names.append(name)
        logger.info(f"Building {len(dims)}-D device mesh with {names}, {dims}")
        names = tuple(names)
        return init_device_mesh(device_type, dims, mesh_dim_names=names)

    @property
    def tp_enabled(self):
        return self.tp > 1

    @property
    def pp_enabled(self):
        return self.pp > 1