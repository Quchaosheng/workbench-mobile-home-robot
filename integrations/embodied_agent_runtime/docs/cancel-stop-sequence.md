# Cancel and STOP Evidence

The runtime treats a cancellation request as an intent. It does not report a
device stop until the Device Bridge has sent a protocol STOP command and
received the matching STOP acknowledgement.

```mermaid
sequenceDiagram
    participant Client
    participant Workflow as ExecuteWorkflow
    participant Task as ExecuteTask
    participant Bridge as ExecuteDeviceCommand
    participant CAN as SocketCAN
    participant Device
    participant History as SQLite history

    Client->>Workflow: cancel goal
    Workflow->>Task: cancel child Action
    Task->>Bridge: cancel child Action
    Bridge->>CAN: send STOP(command_id >= 0x8000)
    CAN->>Device: STOP frame
    alt matching STOP ACK
        Device-->>CAN: STOP ACK(command_id)
        CAN-->>Bridge: STOPPED
        Bridge-->>Task: CANCELED or SAFE_STOP
        Task-->>Workflow: terminal outcome
        Workflow-->>Client: terminal result
        Workflow->>History: exactly-once TaskEvent
    else timeout, reject, or transport error
        Bridge-->>Task: SAFE_STOP/204 or transport error
        Task-->>Workflow: SAFE_STOP
        Workflow-->>Client: terminal result with evidence
        Workflow->>History: exactly-once TaskEvent
    end
```

```mermaid
stateDiagram-v2
    [*] --> Running
    Running --> CancelRequested: user cancel / deadline / shutdown
    CancelRequested --> Stopping: child cancel dispatched
    Stopping --> Canceled: matching STOP ACK and user cancel
    Stopping --> SafeStop: matching STOP ACK after deadline
    Stopping --> SafeStop: STOP ACK missing or rejected
    Canceled --> [*]
    SafeStop --> [*]
```

Evidence boundary:

- `CANCELED` or `SAFE_STOP` proves the software protocol outcome.
- A matching STOP ACK proves the device protocol response, not physical motor
  motion or a measured stopping distance.
- `TaskEvent` is written once with `INSERT OR IGNORE`; a duplicate callback
  cannot create a second terminal history row.
