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

CASE_PASS = "pass"

log = logging.getLogger(__name__)
log.setLevel(logging.INFO)
log.addHandler(logging.StreamHandler())


class TestResult:
    def __init__(self, build_number, job_id, suite, klass, case, status, xml, web_url, ran_at):
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

    def read(self, branch):
        # Only interested in complete runs, not in progress or cancelled
        states = ["passed", "failed"]

        results = defaultdict(list)

        path = f"organizations/{ORGANIZATION}/pipelines/{PIPELINE}/builds"
        params = {'branch': branch, 'state[]': [states]}
        builds = self.get_path(path, params=params)
        log.debug(json.dumps(builds, indent=2))
        for build in builds:
            log.debug(f"{build['finished_at']} {build['number']} {build['state']}")
            for job in build['jobs']:
                name = job['name']
                if DUCKTAPE_JOBS.match(name) is None:
                    continue

                artifacts = self.get(job['artifacts_url']).json()
                artifacts_by_name = dict([(a['filename'], a)
                                          for a in artifacts])
                try:
                    report_artifact = artifacts_by_name['report.xml']
                except KeyError:
                    log.warning(
                        f"No report.xml for build {build['id']} job {job['name']}"
                    )
                    continue

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
                                       test_case.attrib['status'], test_case, job['web_url'], job['finished_at']))

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
                    f"                  in job {mrf.web_url}"
                )


if __name__ == '__main__':
    try:
        branch = sys.argv[1]
    except IndexError:
        branch = DEFAULT_BRANCH

    reader = ResultReader(ResultReaderConfig())
    reader.validate()
    reader.read(branch)
