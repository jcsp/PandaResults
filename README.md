
# PandaResults

This is a program for bulk inspection of redpanda test results, to report
on which tests are unstable.

# Dependencies

* Python 3
* `requests` module

# Configuration

Generate yourself a Buildkite API key:
 - Visit https://buildkite.com/user/api-access-tokens
 - Create a new token with scopes as follows:
   - Organization access: vectorized
   - Read artifacts
   - Read builds
   - Read build logs
   - Read user

Create a `~/.buildkite-auth` file:

	{
	"buildkite_token": "<your token here>",
	"artifact_auth": ["vectorized", "<password>"]
	}

The artifact auth setting is HTTP basic auth for the vectorized
artifact server, see https://vectorizedio.atlassian.net/wiki/spaces/CORE/pages/27033620/Buildkite

# Usage

Examples:

    # Report on the last 24 hours results on `dev`
    python main.py dev 24h

    # Report on the last week on `test-staging`
    python main.py dev 1w

Example output:

	$ python main.py dev 24h
	# ... snip HTTP logs to stderr ...
	Ignored test: NodesDecommissioningTest.test_decommissioning_working_node (26/26 runs ignored)
	Ignored test: PartitionMovementTest.test_dynamic (26/26 runs ignored)
	Ignored test: RpkToolTest.test_consume_from_partition (26/26 runs ignored)
	Ignored test: ArchivalTest.test_isolate (26/26 runs ignored)
	Unstable test: RaftAvailabilityTest.test_one_node_down (2/26 runs failed)
	  Most recent failure at 2021-09-28T07:30:30.421Z: MetricCheckFailed('vectorized_raft_received_vote_requests_total', 2.0, 6.0)
			  in job https://buildkite.com/vectorized/redpanda/builds/2660#7a4a3e18-9000-4faf-ab45-b6ac2d22400d
	Unstable test: ctest.test_cluster_rpunit (2/31 runs failed)
	  Most recent failure at 2021-09-28T07:14:10.501Z: None
			  in job https://buildkite.com/vectorized/redpanda/builds/2660#5be114bc-30b8-44e8-bb57-569f3bc9cbd5
	Unstable test: multiarch-image.multiarch-image (7/11 runs failed)
	  Most recent failure at None: None
			  in job https://buildkite.com/vectorized/redpanda/builds/2661#2307e629-e7ff-4408-b1e5-f43d2fcbbe71
	Unstable test: k8s-operator.k8s-operator (5/11 runs failed)
	  Most recent failure at 2021-09-28T08:52:04.384Z: None
			  in job https://buildkite.com/vectorized/redpanda/builds/2660#3d720045-0651-4fd8-9bec-306291a97888
	Unstable test: k8s-helm.k8s-helm (1/11 runs failed)
	  Most recent failure at 2021-09-28T00:51:11.050Z: None
			  in job https://buildkite.com/vectorized/redpanda/builds/2643#5f019d04-f25d-4b98-b5aa-decbca507dc8


