.. Draft rows for the Scality product documentation (Technical Publications review).
.. Source of the numbers: deploy/ports.yaml. Nothing here is published; each block
.. names the page it is drafted for, in that page's own table format.

Databricks OpenSharing: draft port rows
=======================================

Checked against ``docs-ring-s3c`` development/10 @ a1bd69acd, ``Federation``
@ 9bdd74ea5, ``artesca`` development/4 @ b68f3a8f6: none of 9480, 9481 or 9482 appears
in any port table, ``group_vars/all``, Salt default or firewall page. All three are
below the RING kernel local port range (20480-65001), so no
``net.ipv4.ip_local_reserved_ports_custom`` entry is needed.

RING: ``RING/operation/03_ring_monitoring/configuring_network_ports.rst``
-------------------------------------------------------------------------

For the "Other Components Ports" table, on the node that runs the container.

.. table::
   :widths: auto

   +-------+-----------+--------------------+--------------+----------------+
   | Port  | Connector |      Service       |   Advised    |   Process      |
   |       |           |                    |   check      |   Name         |
   +=======+===========+====================+==============+================+
   | 9480  |           | Databricks         | HTTP check   | isv-opensharing|
   |       |           | OpenSharing        | (GET         |                |
   |       |           | (Delta Sharing     | /healthz)    |                |
   |       |           | protocol)          |              |                |
   +-------+-----------+--------------------+--------------+----------------+
   | 9481  |           | Databricks         | none         | isv-opensharing|
   |       |           | OpenSharing setup  | (loopback    |                |
   |       |           | page (loopback     | only)        |                |
   |       |           | only)              |              |                |
   +-------+-----------+--------------------+--------------+----------------+
   | 9482  |           | Databricks         | HTTP check   | isv-opensharing|
   |       |           | OpenSharing        | (GET         |                |
   |       |           | metrics and health | /readyz)     |                |
   +-------+-----------+--------------------+--------------+----------------+

S3C: ``S3C/installation/install_s3c/Configuring_the_S3_Cluster/Configuring_S3_Connector_Network_Ports.rst``
-----------------------------------------------------------------------------------------------------------

*External Service Ports (INGRESS from a production zone)* — only when the share endpoint
is published without a separate TLS front end; with one, the front end's 443 is the
external port and 9480 is internal.

+---------------+-----------------------+-----------------------------------+
| 9480          | Databricks            | ``SERVER_PORT`` (setup image) or  |
|               | OpenSharing           | ``port:`` in                      |
|               |                       | ``delta-sharing-server.yaml``     |
+---------------+-----------------------+-----------------------------------+

*Internal ports (monitoring)*

+---------------+-----------------------+-----------------------------------+
| 9482          | Databricks            | ``METRICS_PORT``                  |
|               | OpenSharing metrics   |                                   |
+---------------+-----------------------+-----------------------------------+

ARTESCA: ``docs/installation/prerequisites/firewall.rst`` and ``docs/security/firewall.rst``
--------------------------------------------------------------------------------------------

No new row. The sidecar opens no host listener on ARTESCA: 9480 and 9482 are container
ports behind ClusterIP Services, and recipients arrive through the workload-plane
Ingress on TCP 443, already in the *Policy workload-plane* table:

+----------------+-------+----------------+----------------+-----------------+------------+-------------+
| Network        | Role  | Protocol(Port) | Traffic Type   | Direction(s)    | Source     | Destination |
+================+=======+================+================+=================+============+=============+
| workload plane | (any) | TCP(:443)      | Ingress HTTPS  | Ingress         | (any)      | N/A         |
+----------------+-------+----------------+----------------+-----------------+------------+-------------+

ARTESCA: ``docs/installation/prerequisites/dns.rst``
----------------------------------------------------

One hostname row, for the share endpoint's Ingress: ``opensharing.<base-domain-name>``
(proposed; the name is the integration's choice, not an ARTESCA default).

Databricks Serverless prerequisites (both products)
---------------------------------------------------

+-------------------------------+--------------------------------------------------------------+
| Requirement                   | Why                                                          |
+===============================+==============================================================+
| Public DNS name for the share | The recipient dials it from Databricks' network.             |
| endpoint                      |                                                              |
+-------------------------------+--------------------------------------------------------------+
| Public DNS name for the S3    | Presigned URLs carry the ``fs.s3a.endpoint`` host; the       |
| endpoint, registered as a     | recipient fetches the Parquet files from it directly.        |
| rest-endpoint                 |                                                              |
+-------------------------------+--------------------------------------------------------------+
| Publicly trusted TLS          | Databricks rejects self-signed and private-CA chains.        |
| certificate on both names     |                                                              |
+-------------------------------+--------------------------------------------------------------+
| Inbound TCP 443 on both names | Databricks Serverless leaves from its published NCC stable   |
| from the Databricks NCC       | egress ranges; NCC private endpoints do not reach            |
| egress ranges                 | on-premises networks.                                        |
+-------------------------------+--------------------------------------------------------------+
