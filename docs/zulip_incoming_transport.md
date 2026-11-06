# Zulip long-poll transport

The shared Zulip HTTP connector enables Linux TCP keepalive on its sockets:
the first probe follows 10 idle seconds, with five-second retry intervals and
three unanswered probes allowed. HTTP connection pooling keeps its existing
15-second idle reuse deadline. That pool deadline applies between requests;
it does not keep an active long poll alive.

Without TCP probes, a network path can silently discard an idle connection
before Zulip's heartbeat arrives. Zulip can finish the request with HTTP 200
while the receiver never sees its response headers. The receiver then waits
for its existing provider deadline, retries, and receives the accumulated
events. This delays admission even when the durable inbox is empty and message
processing is fast.

Controlled live comparisons reproduced this behavior after 40 idle seconds:
ordinary sockets timed out, while sockets with these keepalive settings
delivered in 0.07–0.22 seconds after the send. Reversing the control order
repeated the result. A 25-second idle control also succeeded without probes.
These bounds establish an idle transport failure, not the identity or exact
timeout of an intermediate network device. They do not establish a regression
in a particular Workspace release.

Long polling, queue ownership, queue IDs, cursor commits, history boundaries
and retry rules retain their existing behavior. No queue reset or periodic
non-blocking polling is required.

## Verification

The unit test checks the actual connected socket's kernel settings and HTTP
connection reuse. The integration regression uses a private Linux network
namespace and an nftables set that forgets idle TCP state after three seconds.
It drops return packets silently after expiry. A real HTTP long poll returns
an event after four seconds; the original client times out and the configured
client receives it. Kernel probe timers are shortened to one second only in
this test. The namespace and its rules disappear when the test exits, without
changing host firewall rules or namespace policy.

The existing receiver integration test separately exercises HTTP, PostgreSQL,
message processing, queue renewal and WebSocket delivery. Production acceptance
requires a separate deployment and fresh delivery checks after the final
restart, including a long poll that has already been idle for 40 seconds.
