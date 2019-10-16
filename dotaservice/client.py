from grpclib.client import Channel
from dotaservice.protos.DotaService_grpc import DotaServiceStub
from dotaservice.protos.DotaService_pb2 import Action
from dotaservice.protos.DotaService_pb2 import Config
# Connect to the DotaService.
env = DotaServiceStub(Channel('127.0.0.1', 13337))

# Get the initial observation.
observation = await env.reset(Config())
for i in range(8):
    # Sample an action from the action protobuf
    action = Action.MoveToLocation(x=.., y=.., z=..)
    # Take an action, returning the resulting observation.
    observation = await env.step(action)