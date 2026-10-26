builtin_interfaces/Time stamp
string task_id
string target_id
# Correlation identity carried through the whole runtime path. Empty when the
# caller supplied none; an identifier, not execution evidence.
string request_id
string workflow_id
# ROS 2 goal identity of the ExecuteTask goal, so a product-side consumer can
# bind this terminal event to the goal it submitted.
string goal_id
uint8 SUCCEEDED=1
uint8 ABORTED=2
uint8 CANCELED=3
uint8 action_status
uint8 outcome
uint16 error_code
uint64 duration_ms
string message
