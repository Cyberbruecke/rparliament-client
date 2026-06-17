# RParliament Client RTR Proxy

The RParliament client functions as an RTR proxy run on the local network. 
While VRPs can be fetched directly from RParliament nodes via RTR-over-TLS, 
some may prefer to place a middleware component between their router and 
the Internet.

The RParliament client provides the added benefit of integrating across the 
outputs of all nodes. Nodes share their perspective on RPKI with each other 
and compute the consensus of the network as they observe it; however, 
depending on timing and network conditions, this can still result in some 
inconsistencies that a client router is exposed to when connecting to any 
one node directly. The RParliament client collects all of these outputs from 
the nodes' RTR endpoints and ranks them by:

- *consensus*: how often a given output is observed (based on its hash)
- *completeness:*: how many VRPs are contained in the output
- *freshness*: how recently the output was observed

The top result is then served by the client's RTR server.

A public instance of this client is deployed at `rtr.rparliament.org:8282`.
