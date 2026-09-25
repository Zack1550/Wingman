from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class CommandStatus(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    COMMAND_STATUS_UNSPECIFIED: _ClassVar[CommandStatus]
    ACCEPTED: _ClassVar[CommandStatus]
    REJECTED_RETRYABLE: _ClassVar[CommandStatus]
    REJECTED_PERMANENT: _ClassVar[CommandStatus]
    ALREADY_APPLIED: _ClassVar[CommandStatus]
    STALE_STATE_VERSION: _ClassVar[CommandStatus]
COMMAND_STATUS_UNSPECIFIED: CommandStatus
ACCEPTED: CommandStatus
REJECTED_RETRYABLE: CommandStatus
REJECTED_PERMANENT: CommandStatus
ALREADY_APPLIED: CommandStatus
STALE_STATE_VERSION: CommandStatus

class Arm(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class Disarm(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class ReturnToLaunch(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class Land(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class SetMode(_message.Message):
    __slots__ = ("mode_name",)
    MODE_NAME_FIELD_NUMBER: _ClassVar[int]
    mode_name: str
    def __init__(self, mode_name: _Optional[str] = ...) -> None: ...

class Takeoff(_message.Message):
    __slots__ = ("target_altitude_m",)
    TARGET_ALTITUDE_M_FIELD_NUMBER: _ClassVar[int]
    target_altitude_m: float
    def __init__(self, target_altitude_m: _Optional[float] = ...) -> None: ...

class GotoPosition(_message.Message):
    __slots__ = ("target_latitude_deg", "target_longitude_deg", "target_altitude_m")
    TARGET_LATITUDE_DEG_FIELD_NUMBER: _ClassVar[int]
    TARGET_LONGITUDE_DEG_FIELD_NUMBER: _ClassVar[int]
    TARGET_ALTITUDE_M_FIELD_NUMBER: _ClassVar[int]
    target_latitude_deg: float
    target_longitude_deg: float
    target_altitude_m: float
    def __init__(self, target_latitude_deg: _Optional[float] = ..., target_longitude_deg: _Optional[float] = ..., target_altitude_m: _Optional[float] = ...) -> None: ...

class VehicleCommand(_message.Message):
    __slots__ = ("operation_id", "vehicle_id", "issued_at_unix_ms", "expected_state_version", "arm", "disarm", "set_mode", "takeoff", "goto_position", "return_to_launch", "land")
    OPERATION_ID_FIELD_NUMBER: _ClassVar[int]
    VEHICLE_ID_FIELD_NUMBER: _ClassVar[int]
    ISSUED_AT_UNIX_MS_FIELD_NUMBER: _ClassVar[int]
    EXPECTED_STATE_VERSION_FIELD_NUMBER: _ClassVar[int]
    ARM_FIELD_NUMBER: _ClassVar[int]
    DISARM_FIELD_NUMBER: _ClassVar[int]
    SET_MODE_FIELD_NUMBER: _ClassVar[int]
    TAKEOFF_FIELD_NUMBER: _ClassVar[int]
    GOTO_POSITION_FIELD_NUMBER: _ClassVar[int]
    RETURN_TO_LAUNCH_FIELD_NUMBER: _ClassVar[int]
    LAND_FIELD_NUMBER: _ClassVar[int]
    operation_id: str
    vehicle_id: str
    issued_at_unix_ms: int
    expected_state_version: int
    arm: Arm
    disarm: Disarm
    set_mode: SetMode
    takeoff: Takeoff
    goto_position: GotoPosition
    return_to_launch: ReturnToLaunch
    land: Land
    def __init__(self, operation_id: _Optional[str] = ..., vehicle_id: _Optional[str] = ..., issued_at_unix_ms: _Optional[int] = ..., expected_state_version: _Optional[int] = ..., arm: _Optional[_Union[Arm, _Mapping]] = ..., disarm: _Optional[_Union[Disarm, _Mapping]] = ..., set_mode: _Optional[_Union[SetMode, _Mapping]] = ..., takeoff: _Optional[_Union[Takeoff, _Mapping]] = ..., goto_position: _Optional[_Union[GotoPosition, _Mapping]] = ..., return_to_launch: _Optional[_Union[ReturnToLaunch, _Mapping]] = ..., land: _Optional[_Union[Land, _Mapping]] = ...) -> None: ...

class CommandAck(_message.Message):
    __slots__ = ("operation_id", "vehicle_id", "status", "reason", "state_version", "acked_at_unix_ms")
    OPERATION_ID_FIELD_NUMBER: _ClassVar[int]
    VEHICLE_ID_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    REASON_FIELD_NUMBER: _ClassVar[int]
    STATE_VERSION_FIELD_NUMBER: _ClassVar[int]
    ACKED_AT_UNIX_MS_FIELD_NUMBER: _ClassVar[int]
    operation_id: str
    vehicle_id: str
    status: CommandStatus
    reason: str
    state_version: int
    acked_at_unix_ms: int
    def __init__(self, operation_id: _Optional[str] = ..., vehicle_id: _Optional[str] = ..., status: _Optional[_Union[CommandStatus, str]] = ..., reason: _Optional[str] = ..., state_version: _Optional[int] = ..., acked_at_unix_ms: _Optional[int] = ...) -> None: ...

class VehicleTelemetry(_message.Message):
    __slots__ = ("vehicle_id", "sampled_at_unix_ms", "latitude_deg", "longitude_deg", "altitude_m", "ground_speed_mps", "heading_deg", "flight_mode", "is_armed", "gps_fix_type", "battery_percent", "state_version")
    VEHICLE_ID_FIELD_NUMBER: _ClassVar[int]
    SAMPLED_AT_UNIX_MS_FIELD_NUMBER: _ClassVar[int]
    LATITUDE_DEG_FIELD_NUMBER: _ClassVar[int]
    LONGITUDE_DEG_FIELD_NUMBER: _ClassVar[int]
    ALTITUDE_M_FIELD_NUMBER: _ClassVar[int]
    GROUND_SPEED_MPS_FIELD_NUMBER: _ClassVar[int]
    HEADING_DEG_FIELD_NUMBER: _ClassVar[int]
    FLIGHT_MODE_FIELD_NUMBER: _ClassVar[int]
    IS_ARMED_FIELD_NUMBER: _ClassVar[int]
    GPS_FIX_TYPE_FIELD_NUMBER: _ClassVar[int]
    BATTERY_PERCENT_FIELD_NUMBER: _ClassVar[int]
    STATE_VERSION_FIELD_NUMBER: _ClassVar[int]
    vehicle_id: str
    sampled_at_unix_ms: int
    latitude_deg: float
    longitude_deg: float
    altitude_m: float
    ground_speed_mps: float
    heading_deg: float
    flight_mode: str
    is_armed: bool
    gps_fix_type: int
    battery_percent: float
    state_version: int
    def __init__(self, vehicle_id: _Optional[str] = ..., sampled_at_unix_ms: _Optional[int] = ..., latitude_deg: _Optional[float] = ..., longitude_deg: _Optional[float] = ..., altitude_m: _Optional[float] = ..., ground_speed_mps: _Optional[float] = ..., heading_deg: _Optional[float] = ..., flight_mode: _Optional[str] = ..., is_armed: _Optional[bool] = ..., gps_fix_type: _Optional[int] = ..., battery_percent: _Optional[float] = ..., state_version: _Optional[int] = ...) -> None: ...

class CommandStatusQuery(_message.Message):
    __slots__ = ("operation_id",)
    OPERATION_ID_FIELD_NUMBER: _ClassVar[int]
    operation_id: str
    def __init__(self, operation_id: _Optional[str] = ...) -> None: ...

class CommandStatusResponse(_message.Message):
    __slots__ = ("operation_id", "is_known", "ack")
    OPERATION_ID_FIELD_NUMBER: _ClassVar[int]
    IS_KNOWN_FIELD_NUMBER: _ClassVar[int]
    ACK_FIELD_NUMBER: _ClassVar[int]
    operation_id: str
    is_known: bool
    ack: CommandAck
    def __init__(self, operation_id: _Optional[str] = ..., is_known: _Optional[bool] = ..., ack: _Optional[_Union[CommandAck, _Mapping]] = ...) -> None: ...

class ClientRequest(_message.Message):
    __slots__ = ("command", "status_query")
    COMMAND_FIELD_NUMBER: _ClassVar[int]
    STATUS_QUERY_FIELD_NUMBER: _ClassVar[int]
    command: VehicleCommand
    status_query: CommandStatusQuery
    def __init__(self, command: _Optional[_Union[VehicleCommand, _Mapping]] = ..., status_query: _Optional[_Union[CommandStatusQuery, _Mapping]] = ...) -> None: ...

class GatewayResponse(_message.Message):
    __slots__ = ("ack", "status")
    ACK_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    ack: CommandAck
    status: CommandStatusResponse
    def __init__(self, ack: _Optional[_Union[CommandAck, _Mapping]] = ..., status: _Optional[_Union[CommandStatusResponse, _Mapping]] = ...) -> None: ...
