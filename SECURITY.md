# Security and responsible reporting

This server controls physical equipment. Deploy behind authenticated SSH with a
dedicated restricted key, verified host identity and a non-root instrument account.
Use `--allow-control` deliberately. Read operations can configure transfer state;
they are not an electrical isolation boundary. No anonymous HTTP service is provided.

Artifact IDs are bounded and payload hashes checked, but the host account remains
trusted. Different clients sharing that account can access its captures. Linux locks
do not govern the front panel, unrelated applications, other users or USB aliases.
Do not run competing controllers. Keep credentials, firmware backups and private
measurement data outside the public checkout.

Do not disclose credentials or exploitable details in public issues. If the hosting
repository offers private vulnerability reporting, use it; otherwise establish a
private maintainer contact before sharing sensitive details. No monitored security
address or private-reporting service is claimed by this unpublished candidate.
There is no promised security response SLA or long-term version support policy.
