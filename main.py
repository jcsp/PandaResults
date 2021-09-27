import sys
import os
import logging
import json
import re
from xml.etree import ElementTree
from collections import defaultdict

import requests

CONFIG_PATH = "~/.buildkite-auth"
BASE_URL = "https://api.buildkite.com/v2/"
ORGANIZATION = "vectorized"
PIPELINE = "redpanda"
DUCKTAPE_JOBS = re.compile(r"(debug|release)-.+")
DEFAULT_BRANCH = 'dev'

# Ducktape's strings for test status
CASE_PASS = "pass"
CASE_FAIL = "fail"

# Buildkite's strings for job status
JOB_FAILED = 'failed'
JOB_PASSED = 'passed'

log = logging.getLogger(__name__)
log.setLevel(logging.INFO)
handler = logging.StreamHandler()
formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
handler.setFormatter(formatter)
log.addHandler(handler)


class TestResult:
    def __init__(self, build_number, job_id, suite, klass, case, status, xml,
                 web_url, ran_at):
        self.build_number = build_number
        self.job_id = job_id
        self.suite = suite
        self.klass = klass
        self.case = case
        self.status = status
        self.xml = xml
        self.web_url = web_url
        self.ran_at = ran_at

    @property
    def timestamp(self):
        return self.ran_at

    @property
    def message(self):
        if self.xml is None:
            return None

        for child in self.xml:
            if child.tag == "failure":
                return child.attrib['message']

        # No specific failure element?  Return empty
        return ""


class TestCase:
    def __init__(self, suite, klass, case):
        self.suite = suite
        self.klass = klass
        self.case = case

    @property
    def tuple(self):
        return (self.suite, self.klass, self.case)

    def __eq__(self, other):
        return self.tuple == other.tuple

    def __hash__(self):
        return hash(self.tuple)

    def __str__(self):
        # While we retain both suite and klass for correctness, in practice
        # human eyeballs just want to know the class+case
        return f"{self.klass}.{self.case}"


class ResultReaderConfig:
    def __init__(self):
        j = json.load(open(os.path.expanduser(CONFIG_PATH)))
        self.buildkite_token = j['buildkite_token']
        self.artifact_auth = tuple(j['artifact_auth'])


class ResultReader:
    def __init__(self, config):
        self.config = config

    def validate(self):
        """
        Raise exception if we can't access the API using the token provided
        """
        self.get_path("user")

    def get(self, url, *args, **kwargs):
        headers = {'Authorization': f'Bearer {self.config.buildkite_token}'}
        r = requests.get(url, headers=headers, *args, **kwargs)
        log.info(f"[{r.status_code}] {url}")
        r.raise_for_status()
        return r

    def get_artifact(self, url):
        r = requests.get(url, auth=self.config.artifact_auth)
        r.raise_for_status()
        log.info(f"[{r.status_code}] {url}")
        return r.text

    def get_path(self, path, *args, **kwargs):
        return self.get(BASE_URL + path, *args, **kwargs).json()

    def _read_ducktape_results(self, results, build, job):
        any_failed = False

        artifacts = self.get(job['artifacts_url']).json()
        artifacts_by_name = dict([(a['filename'], a) for a in artifacts])
        try:
            report_artifact = artifacts_by_name['report.xml']
        except KeyError:
            log.warning(
                f"No report.xml for build {build['id']} job {job['name']}")
            return

        # The buildkite API gives us a buildkite location for download, but
        # it will redirect to a location on Vectorized's proxy that we must
        # authenticate with differently.
        redirect_r = self.get(report_artifact['download_url'],
                              allow_redirects=False)
        download_url = redirect_r.headers['Location']

        # Example:
        # <testsuites name="ducktape" time="728.1522748470306" tests="102" disabled="0" errors="0" failures="2">
        # <testsuite name="rptest.tests.group_membership_test" tests="1" disabled="0" errors="0" failures="0"
        # skipped="0"><testcase name="test_list_groups" classname="ListGroupsReplicationFactorTest"
        # time="18.697760105133057" status="pass" assertions="" /></testsuite>...

        raw_xml = self.get_artifact(download_url)
        report = ElementTree.fromstring(raw_xml)
        for test_suite in report:
            assert test_suite.tag == 'testsuite'
            for test_case in test_suite:
                assert test_case.tag == 'testcase'
                class_name = test_case.attrib['classname']
                name = test_case.attrib['name']
                status = test_case.attrib['status']
                any_failed = any_failed or status == CASE_FAIL
                log.debug(f"{class_name}.{name} {status}")

                results[TestCase(
                    test_suite.attrib['name'],
                    test_case.attrib['classname'],
                    test_case.attrib['name'],
                )].append(
                    TestResult(build['number'], job['id'],
                               test_suite.attrib['name'],
                               test_case.attrib['classname'],
                               test_case.attrib['name'],
                               test_case.attrib['status'], test_case,
                               job['web_url'], job['finished_at']))

        return any_failed

    def _read_ctest_results(self, results, build, job):
        """
        CMake only added machine readable output in 3.21, which is not available on most
        distros.  So for the moment, scrape unit test output from the log.
        """
        r = self.get(job['raw_log_url'])

        CTEST_RESULT_RE = re.compile(
            r"^\d+/\d+ Test #\d+: ([^\s]+) .*(Failed|Passed)\s+\d.*")

        any_failed = False

        # Carry partial lines at end of chunk
        partial = None

        for chunk in r.iter_content(chunk_size=32768):
            lines = chunk.split(b"\n")

            if partial is not None:
                composed = partial + lines[0]
                partial = lines[-1]
                lines = [composed] + lines[1:-1]

            ##print(f"partial = {partial}")
            #print(lines)

            # Examples:
            # 58/64 Test #58: test_archival_service_rpunit ......................***Failed   13.35 sec^M
            # 57/64 Test #57: s3_single_thread_rpunit ...........................   Passed    1.56 sec^M
            for line in lines[:-1]:
                line = line.strip()
                line = line.decode('utf-8')
                match = CTEST_RESULT_RE.match(line)
                if match:
                    test_name, test_result = match.groups()
                    status = CASE_PASS if test_result == "Passed" else CASE_FAIL
                    any_failed = any_failed or status == CASE_FAIL

                    log.debug(f"Unit test {test_name} {status}")
                    results[TestCase("ctest", "ctest", test_name)].append(
                        TestResult(build['number'], job['id'], "ctest",
                                   "ctest", test_name, status, None,
                                   job['web_url'], job['finished_at']))

        return any_failed

    def read(self, branch):
        # Only interested in complete runs, not in progress or cancelled
        states = ["passed", "failed"]

        results = defaultdict(list)

        path = f"organizations/{ORGANIZATION}/pipelines/{PIPELINE}/builds"
        params = {'branch': branch, 'state[]': [states]}
        builds = self.get_path(path, params=params)
        log.debug(json.dumps(builds, indent=2))
        for build in builds:
            log.debug(
                f"{build['finished_at']} {build['number']} {build['state']}")
            for job in build['jobs']:
                name = job['name']
                if DUCKTAPE_JOBS.match(name) is None:
                    continue

                ducktape_fail = self._read_ducktape_results(
                    results, build, job)
                ctest_fail = self._read_ctest_results(results, build, job)

                if job['state'] != JOB_PASSED and not ducktape_fail and not ctest_fail:
                    # If a job is failed, we expect to have seen some test failures.  Otherwise log a warning
                    # to avoid wrongly ignoring failed jobs.
                    log.warning(
                        f"Unexplained failure (state={job['state']}) on build {build['number']} job {job['name']}"
                    )
                elif job['state'] == JOB_PASSED and (ducktape_fail
                                                     or ctest_fail):
                    # Sanity check that our CI logic is correctly reporting the result of jobs
                    log.warning(
                        f"Job passed but has test failures (state={job['state']}) on build {build['number']} job {job['name']}"
                    )

        if not results:
            # Treat this as an error to avoid falsely claiming everything is fine if
            # something goes wrong with the results scraping.
            log.error("No results found")
            sys.exit(-1)

        for case, result_list in results.items():
            failures = [r for r in result_list if r.status != CASE_PASS]
            if failures:
                print(
                    f"Unstable test: {case} ({len(failures)}/{len(result_list)} runs failed)"
                )
                mrf = failures[0]
                print(
                    f"  Most recent failure at {mrf.timestamp}: {mrf.message}\n"
                    f"                  in job {mrf.web_url}")


if __name__ == '__main__':
    try:
        branch = sys.argv[1]
    except IndexError:
        branch = DEFAULT_BRANCH

    reader = ResultReader(ResultReaderConfig())
    reader.validate()
    reader.read(branch)
