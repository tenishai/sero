"""Tests for the kubectl apply/read-back/probe layer (D18, D18a).

Nothing here shells out: every function takes the subprocess ``run`` seam as a
keyword argument, and the tests pass a recorder that returns canned kubectl
output. The documents are brand-neutral (`ns-a`, `sample`) and carry no values.
"""

import json
import subprocess

import pytest
import yaml

from fake_cluster import FakeCluster
from ladon.install import k8s
from ladon.install.k8s import (
    ESOInstall,
    ESONotInstalledError,
    K8sError,
    apply_documents,
    detect_eso,
    eso_api_version,
    read_back,
    verify,
)


class _Result:
    """The subset of CompletedProcess the k8s helpers read."""

    def __init__(self, argv, *, returncode=0, stdout="", stderr=""):
        self.args = list(argv)
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class FakeRunner:
    """The subprocess seam: records every call, replays scripted output.

    ``handler`` receives ``(argv, stdin)`` and returns a ``_Result``; the simple
    cases just set one canned result for every call.
    """

    def __init__(self, *, stdout="", stderr="", returncode=0, handler=None):
        self.calls: list[list[str]] = []
        self.stdins: list[str | None] = []
        self._stdout = stdout
        self._stderr = stderr
        self._returncode = returncode
        self._handler = handler

    def __call__(self, argv, *, stdin=None):
        argv = list(argv)
        self.calls.append(argv)
        self.stdins.append(stdin)
        if self._handler is not None:
            return self._handler(argv, stdin)
        return _Result(argv, returncode=self._returncode, stdout=self._stdout, stderr=self._stderr)

    @property
    def argv(self) -> list[str]:
        """The only call's argv, for the single-command cases."""
        assert len(self.calls) == 1, self.calls
        return self.calls[0]


def service_account_doc():
    """The per-app service account, in Ladon's own namespace (D18a)."""
    return {
        "apiVersion": "v1",
        "kind": "ServiceAccount",
        "metadata": {
            "name": "eso-ns-a-sample",
            "namespace": "ladon",
            "labels": {"app.kubernetes.io/managed-by": "ladon"},
        },
        "automountServiceAccountToken": False,
    }


def store_doc():
    """The per-app ClusterSecretStore: cluster-scoped, so no namespace."""
    return {
        "apiVersion": "external-secrets.io/v1",
        "kind": "ClusterSecretStore",
        "metadata": {"name": "ladon-ns-a-sample"},
        "spec": {
            "provider": {
                "vault": {
                    "server": "https://vault.example.com",
                    "path": "kv",
                    "namespace": "ns-a",
                }
            },
            "conditions": [
                {"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "ns-a"}}}
            ],
        },
    }


def both_docs():
    return [service_account_doc(), store_doc()]


# apply_documents -----------------------------------------------------------


def test_apply_argv_is_server_side_and_dry_run_by_default():
    runner = FakeRunner(stdout="serviceaccount/eso-ns-a-sample created (server dry run)\n")
    apply_documents([service_account_doc()], run=runner)
    assert runner.argv == [
        "kubectl",
        "apply",
        "--server-side",
        "--field-manager=ladon",
        "--dry-run=server",
        "-f",
        "-",
    ]


def test_apply_never_forces_conflicts():
    runner = FakeRunner(stdout="serviceaccount/eso-ns-a-sample created\n")
    apply_documents([service_account_doc()], dry_run=False, run=runner)
    assert not any(a.startswith("--force-conflicts") for a in runner.argv)


def test_apply_drops_the_dry_run_flag_when_applying():
    runner = FakeRunner(stdout="serviceaccount/eso-ns-a-sample created\n")
    report = apply_documents([service_account_doc()], dry_run=False, run=runner)
    assert not any(a.startswith("--dry-run") for a in runner.argv)
    assert report.dry_run is False


def test_apply_honours_the_field_manager():
    runner = FakeRunner(stdout="serviceaccount/eso-ns-a-sample created\n")
    apply_documents([service_account_doc()], field_manager="sample-manager", run=runner)
    assert "--field-manager=sample-manager" in runner.argv


def test_apply_sends_one_yaml_stream_on_stdin():
    runner = FakeRunner(
        stdout=(
            "serviceaccount/eso-ns-a-sample created\n"
            "clustersecretstore.external-secrets.io/ladon-ns-a-sample created\n"
        )
    )
    apply_documents(both_docs(), run=runner)
    assert list(yaml.safe_load_all(runner.stdins[0])) == both_docs()


def test_report_names_every_document_and_its_action():
    runner = FakeRunner(
        stdout=(
            "serviceaccount/eso-ns-a-sample unchanged\n"
            "clustersecretstore.external-secrets.io/ladon-ns-a-sample configured\n"
        )
    )
    report = apply_documents(both_docs(), run=runner)
    assert report.lines() == [
        "ServiceAccount/ladon/eso-ns-a-sample: unchanged",
        "ClusterSecretStore/ladon-ns-a-sample: configured",
    ]
    assert report.blocked == ()


def test_server_dry_run_suffix_is_parsed_away():
    runner = FakeRunner(stdout="serviceaccount/eso-ns-a-sample created (server dry run)\n")
    report = apply_documents([service_account_doc()], run=runner)
    assert report.results[0].action == "created"


def test_serverside_applied_is_reported_as_configured():
    # kubectl's server-side apply prints one verb for create and update alike.
    runner = FakeRunner(stdout="serviceaccount/eso-ns-a-sample serverside-applied\n")
    report = apply_documents([service_account_doc()], run=runner)
    assert report.results[0].action == "configured"


# The real shape of a server-side apply conflict: one error line, then help
# text whose column-zero bullets are not errors.
SSA_CONFLICT = (
    'error: Apply failed with 1 conflict: conflict with "other-manager" using '
    "external-secrets.io/v1: .spec.provider.vault.server\n"
    "Please review the fields above--they currently have other managers. Here\n"
    "are the ways you can resolve this warning:\n"
    "* If you intend to manage all of these fields, please re-run the apply\n"
    "  command with the `--force-conflicts` flag.\n"
    "* If you do not intend to manage all of the fields, please edit your\n"
    "  manifest to remove references to the fields that should keep their\n"
    "  current managers.\n"
)


def test_a_pure_server_side_apply_conflict_blocks_the_unreported_document():
    runner = FakeRunner(
        stdout="serviceaccount/eso-ns-a-sample unchanged\n",
        stderr=SSA_CONFLICT,
        returncode=1,
    )
    report = apply_documents(both_docs(), run=runner)
    assert report.results[0].action == "unchanged"
    assert report.results[1].action == "blocked"
    assert "conflict" in report.results[1].reason
    # Only the first line of the message is carried through.
    assert "Please review" not in report.results[1].reason
    assert [r.name for r in report.blocked] == ["ladon-ns-a-sample"]


def test_an_aggregated_list_of_conflicts_blocks():
    runner = FakeRunner(
        stdout="serviceaccount/eso-ns-a-sample unchanged\n",
        stderr=(
            "error: Apply failed with 2 conflicts:\n"
            '\t* conflict with "other-manager" using external-secrets.io/v1: .spec.provider\n'
            '\t* conflict with "other-manager" using external-secrets.io/v1: .spec.conditions\n'
        ),
        returncode=1,
    )
    report = apply_documents(both_docs(), run=runner)
    assert [r.action for r in report.results] == ["unchanged", "blocked"]


def test_a_forbidden_error_alongside_a_conflict_raises():
    # A denial sharing the output with a conflict must not be swallowed as a
    # blocked document: the apply did not do what it was asked.
    runner = FakeRunner(
        stdout="serviceaccount/eso-ns-a-sample unchanged\n",
        stderr=(
            'error: Apply failed with 1 conflict: conflict with "other-manager"\n'
            "Error from server (Forbidden): clustersecretstores.external-secrets.io "
            "is forbidden: User cannot patch resource\n"
        ),
        returncode=1,
    )
    with pytest.raises(K8sError, match="Forbidden|conflict"):
        apply_documents(both_docs(), run=runner)


def test_a_webhook_denial_that_merely_says_conflicts_raises():
    runner = FakeRunner(
        stderr=(
            "Error from server (InternalError): error when applying patch: admission webhook "
            '"v.example.com" denied the request: the resource conflicts with an existing rule\n'
        ),
        returncode=1,
    )
    with pytest.raises(K8sError, match="kubectl apply failed"):
        apply_documents(both_docs(), run=runner)


def test_an_aggregated_list_with_one_non_conflict_item_raises():
    runner = FakeRunner(
        stderr=(
            "error: Apply failed with 2 conflicts:\n"
            '\t* conflict with "other-manager" using v1: .metadata.labels\n'
            '\t* serviceaccounts "eso-ns-a-sample" is forbidden\n'
        ),
        returncode=1,
    )
    with pytest.raises(K8sError):
        apply_documents(both_docs(), run=runner)


def test_a_failure_with_no_recognisable_error_line_raises():
    # Fail closed: an exit code we cannot explain is never a blocked document.
    runner = FakeRunner(stderr="something went wrong\n", returncode=1)
    with pytest.raises(K8sError):
        apply_documents(both_docs(), run=runner)


def test_any_other_failure_raises_with_only_the_first_stderr_line():
    runner = FakeRunner(
        stderr="error: the server could not find the requested resource\ntoken=sample-value\n",
        returncode=1,
    )
    with pytest.raises(K8sError) as excinfo:
        apply_documents(both_docs(), run=runner)
    assert "could not find the requested resource" in str(excinfo.value)
    assert "sample-value" not in str(excinfo.value)


def test_a_long_error_line_is_truncated():
    runner = FakeRunner(stderr="error: " + "x" * 500 + "\n", returncode=1)
    with pytest.raises(K8sError) as excinfo:
        apply_documents(both_docs(), run=runner)
    assert len(str(excinfo.value)) < 300


def test_a_conflict_does_not_blame_a_document_whose_name_is_a_prefix():
    # "sample" is a substring of "sample-two"; only the document kubectl printed
    # no result line for is blocked.
    first = service_account_doc()
    first["metadata"]["name"] = "eso-sample"
    second = service_account_doc()
    second["metadata"]["name"] = "eso-sample-two"
    runner = FakeRunner(
        stdout="serviceaccount/eso-sample configured\n",
        stderr=(
            'error: Apply failed with 1 conflict on serviceaccount "eso-sample-two": '
            'conflict with "other-manager"\n'
        ),
        returncode=1,
    )
    report = apply_documents([first, second], run=runner)
    assert [r.action for r in report.results] == ["configured", "blocked"]


def test_one_result_line_does_not_answer_for_two_namespaces():
    # Result lines carry no namespace, so the same kind and name in two
    # namespaces must not share one line.
    first = service_account_doc()
    second = service_account_doc()
    second["metadata"]["namespace"] = "ns-a"
    runner = FakeRunner(stdout="serviceaccount/eso-ns-a-sample configured\n")
    report = apply_documents([first, second], run=runner)
    assert [r.action for r in report.results] == ["configured", "blocked"]
    assert report.results[1].namespace == "ns-a"


def test_two_result_lines_answer_for_two_documents_in_order():
    first = service_account_doc()
    second = service_account_doc()
    second["metadata"]["namespace"] = "ns-a"
    runner = FakeRunner(
        stdout=(
            "serviceaccount/eso-ns-a-sample created\nserviceaccount/eso-ns-a-sample unchanged\n"
        )
    )
    report = apply_documents([first, second], run=runner)
    assert [r.action for r in report.results] == ["created", "unchanged"]


def test_a_document_kubectl_did_not_report_is_blocked():
    runner = FakeRunner(stdout="serviceaccount/eso-ns-a-sample created\n")
    report = apply_documents(both_docs(), run=runner)
    assert report.results[1].action == "blocked"
    assert report.results[1].reason


def test_no_documents_runs_no_command():
    runner = FakeRunner()
    report = apply_documents([], run=runner)
    assert runner.calls == []
    assert report.results == ()


def test_a_document_without_a_name_is_refused():
    runner = FakeRunner()
    with pytest.raises(K8sError, match="kind and metadata.name"):
        apply_documents([{"apiVersion": "v1", "kind": "ServiceAccount"}], run=runner)


def test_a_namespaced_kind_without_a_namespace_is_refused():
    doc = service_account_doc()
    del doc["metadata"]["namespace"]
    with pytest.raises(K8sError, match="cluster-scoped"):
        apply_documents([doc], run=FakeRunner())


def test_a_cluster_scoped_kind_without_a_namespace_is_accepted():
    runner = FakeRunner(stdout="clustersecretstore.external-secrets.io/ladon-ns-a-sample created\n")
    report = apply_documents([store_doc()], run=runner)
    assert report.results[0].action == "created"


# read_back -----------------------------------------------------------------


def test_read_back_passes_the_namespace_for_a_namespaced_document():
    runner = FakeRunner(stdout=json.dumps(service_account_doc()))
    read_back([service_account_doc()], run=runner)
    assert runner.argv == [
        "kubectl",
        "get",
        "serviceaccount/eso-ns-a-sample",
        "-n",
        "ladon",
        "-o",
        "json",
    ]


def test_read_back_omits_the_namespace_for_a_cluster_scoped_document():
    runner = FakeRunner(stdout=json.dumps(store_doc()))
    read_back([store_doc()], run=runner)
    assert runner.argv == [
        "kubectl",
        "get",
        "clustersecretstore.external-secrets.io/ladon-ns-a-sample",
        "-o",
        "json",
    ]


def test_read_back_returns_the_live_objects_in_order():
    docs = both_docs()
    runner = FakeRunner(handler=lambda argv, stdin: _Result(argv, stdout=json.dumps(docs[0])))
    live = read_back([docs[0]], run=runner)
    assert live == [docs[0]]


def test_read_back_failure_raises_with_only_the_first_stderr_line():
    runner = FakeRunner(
        stderr='Error from server (NotFound): serviceaccounts "eso-ns-a-sample" not found\nmore\n',
        returncode=1,
    )
    with pytest.raises(K8sError) as excinfo:
        read_back([service_account_doc()], run=runner)
    assert "not found" in str(excinfo.value)
    assert "more" not in str(excinfo.value)


def test_read_back_refuses_output_that_is_not_json():
    runner = FakeRunner(stdout="not json")
    with pytest.raises(K8sError, match="not JSON"):
        read_back([service_account_doc()], run=runner)


# verify --------------------------------------------------------------------


def live_from(doc, **extra):
    """A live object built from a desired document, plus server-set noise."""
    live = json.loads(json.dumps(doc))
    live.setdefault("metadata", {}).update(
        {"uid": "0000", "resourceVersion": "1", "creationTimestamp": "2026-01-01T00:00:00Z"}
    )
    live["metadata"].setdefault("labels", {})["kubernetes.io/bootstrapping"] = "sample"
    live.update(extra)
    return live


def test_verify_passes_when_the_live_object_carries_what_we_set():
    docs = both_docs()
    assert verify(docs, [live_from(d) for d in docs]) == []


def test_verify_ignores_fields_the_document_does_not_set():
    doc = store_doc()
    live = live_from(doc)
    live["status"] = {"conditions": [{"type": "Ready"}]}
    live["spec"]["provider"]["vault"]["version"] = "v2"
    assert verify([doc], [live]) == []


def test_verify_reports_a_changed_spec_field_and_never_its_value():
    doc = store_doc()
    live = live_from(doc)
    live["spec"]["provider"]["vault"]["server"] = "https://elsewhere.example.com"
    problems = verify([doc], [live])
    assert problems == [
        "ClusterSecretStore/ladon-ns-a-sample: spec.provider.vault.server differs "
        "from the live object"
    ]
    # The field path is named; neither the desired nor the live value is.
    assert "example.com" not in problems[0]


def test_verify_reports_a_missing_label():
    doc = service_account_doc()
    live = live_from(doc)
    del live["metadata"]["labels"]["app.kubernetes.io/managed-by"]
    problems = verify([doc], [live])
    assert problems == [
        "ServiceAccount/eso-ns-a-sample: metadata.labels.app.kubernetes.io/managed-by "
        "is missing from the live object"
    ]


def test_verify_checks_the_automount_flag():
    doc = service_account_doc()
    live = live_from(doc)
    live["automountServiceAccountToken"] = True
    problems = verify([doc], [live])
    assert problems == [
        "ServiceAccount/eso-ns-a-sample: automountServiceAccountToken differs from the live object"
    ]


def test_verify_compares_conditions_in_order():
    doc = store_doc()
    doc["spec"]["conditions"].append(
        {"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "ns-b"}}}
    )
    live = live_from(doc)
    live["spec"]["conditions"].reverse()
    problems = verify([doc], [live])
    assert len(problems) == 2
    assert all("spec.conditions[" in p for p in problems)


def test_verify_reports_an_extra_condition_on_the_live_object():
    doc = store_doc()
    live = live_from(doc)
    live["spec"]["conditions"].append(
        {"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "ns-b"}}}
    )
    problems = verify([doc], [live])
    assert problems == [
        "ClusterSecretStore/ladon-ns-a-sample: spec.conditions has 1 entry, the live object has 2"
    ]


def test_verify_is_exact_inside_a_store_condition():
    # A key added beside Ladon's selector keeps the list length and every field
    # Ladon set, so a subset compare reads clean while the store has been opened
    # to a namespace Ladon never admitted.
    doc = store_doc()
    live = live_from(doc)
    live["spec"]["conditions"][0]["namespaces"] = ["ns-other"]
    problems = verify([doc], [live])
    assert problems == [
        "ClusterSecretStore/ladon-ns-a-sample: spec.conditions[0].namespaces is not set by Ladon"
    ]
    assert "ns-other" not in problems[0]


def test_verify_is_exact_inside_the_provider_auth_block():
    doc = store_doc()
    doc["spec"]["provider"]["vault"]["auth"] = {"jwt": {"role": "eso-ns-a-sample"}}
    live = live_from(doc)
    live["spec"]["provider"]["vault"]["auth"]["tokenSecretRef"] = {"name": "sample"}
    problems = verify([doc], [live])
    assert problems == [
        "ClusterSecretStore/ladon-ns-a-sample: spec.provider.vault.auth.tokenSecretRef "
        "is not set by Ladon"
    ]


def test_verify_stays_a_subset_outside_the_strict_paths():
    # Everywhere else a live object may carry what the API server, a controller
    # or another manager put there; only what Ladon asked for is checked.
    doc = store_doc()
    live = live_from(doc)
    live["spec"]["refreshInterval"] = "1h"
    assert verify([doc], [live]) == []


def test_verify_reports_a_document_missing_from_the_read_back():
    docs = both_docs()
    problems = verify(docs, [live_from(docs[0])])
    assert problems == [
        "ClusterSecretStore/ladon-ns-a-sample: not present in the live objects read back"
    ]


# eso_api_version -----------------------------------------------------------


def _api_version_handler(*, resources="externalsecrets\nclustersecretstores\n", storage="v1"):
    def handler(argv, stdin):
        if argv[1] == "api-resources":
            return _Result(argv, stdout=resources)
        return _Result(argv, stdout=storage)

    return handler


def test_eso_api_version_probes_both_and_returns_the_storage_version():
    runner = FakeRunner(handler=_api_version_handler())
    assert eso_api_version(run=runner) == "v1"
    assert runner.calls == [
        ["kubectl", "api-resources", "--api-group=external-secrets.io", "-o", "name"],
        [
            "kubectl",
            "get",
            "crd",
            "externalsecrets.external-secrets.io",
            "-o",
            "jsonpath={.spec.versions[?(@.storage==true)].name}",
        ],
    ]


def test_eso_api_version_returns_the_older_served_version():
    runner = FakeRunner(handler=_api_version_handler(storage="v1beta1"))
    assert eso_api_version(run=runner) == "v1beta1"


def test_eso_api_version_raises_a_distinguishable_error_when_the_crd_is_absent():
    # The absent case is its own error type: it is the one failure a caller may
    # answer by installing the chart, rather than by stopping.
    runner = FakeRunner(handler=_api_version_handler(resources="\n"))
    with pytest.raises(ESONotInstalledError, match="not installed"):
        eso_api_version(run=runner)
    assert issubclass(ESONotInstalledError, K8sError)
    # The absent case is answered by the first probe alone.
    assert len(runner.calls) == 1


def test_eso_api_version_raises_when_the_crd_read_fails():
    def handler(argv, stdin):
        if argv[1] == "api-resources":
            return _Result(argv, stdout="externalsecrets\n")
        return _Result(argv, returncode=1, stderr="Error from server (NotFound)\nnoise\n")

    with pytest.raises(K8sError) as excinfo:
        eso_api_version(run=FakeRunner(handler=handler))
    assert "noise" not in str(excinfo.value)


def test_eso_api_version_raises_when_no_version_is_marked_storage():
    runner = FakeRunner(handler=_api_version_handler(storage="  \n"))
    with pytest.raises(K8sError, match="storage version"):
        eso_api_version(run=runner)


# detect_eso ----------------------------------------------------------------


def deployment(*, namespace="external-secrets", version_label="v0.19.2", image=None):
    metadata = {"name": "external-secrets", "namespace": namespace, "labels": {}}
    if version_label is not None:
        metadata["labels"]["app.kubernetes.io/version"] = version_label
    return {
        "kind": "Deployment",
        "metadata": metadata,
        "spec": {
            "template": {
                "spec": {
                    "serviceAccountName": "external-secrets",
                    "containers": [
                        {"name": "external-secrets", "image": image or "example/eso:v1"}
                    ],
                }
            }
        },
    }


def test_detect_eso_queries_every_namespace_by_the_name_label():
    runner = FakeRunner(stdout=json.dumps({"items": []}))
    detect_eso(run=runner)
    assert runner.argv == [
        "kubectl",
        "get",
        "deploy",
        "-A",
        "-l",
        "app.kubernetes.io/name=external-secrets",
        "-o",
        "json",
    ]


def test_detect_eso_returns_none_when_no_operator_is_installed():
    assert detect_eso(run=FakeRunner(stdout=json.dumps({"items": []}))) is None


def test_detect_eso_reads_the_version_label():
    runner = FakeRunner(stdout=json.dumps({"items": [deployment(namespace="ns-a")]}))
    assert detect_eso(run=runner) == ESOInstall(
        namespace="ns-a", service_account="external-secrets", version="v0.19.2"
    )


def test_detect_eso_falls_back_to_the_image_tag():
    runner = FakeRunner(
        stdout=json.dumps(
            {"items": [deployment(version_label=None, image="registry.example.com/eso:v0.18.1")]}
        )
    )
    assert detect_eso(run=runner).version == "v0.18.1"


def test_detect_eso_reports_an_unknown_version_when_nothing_names_one():
    runner = FakeRunner(
        stdout=json.dumps(
            {"items": [deployment(version_label=None, image="registry.example.com/eso@sha256:abc")]}
        )
    )
    assert detect_eso(run=runner).version == "unknown"


def test_detect_eso_raises_when_kubectl_fails():
    runner = FakeRunner(stderr="error: you do not have permission\nnoise\n", returncode=1)
    with pytest.raises(K8sError) as excinfo:
        detect_eso(run=runner)
    assert "noise" not in str(excinfo.value)


def test_detect_eso_refuses_more_than_one_operator():
    runner = FakeRunner(
        stdout=json.dumps({"items": [deployment(namespace="ns-a"), deployment(namespace="ns-b")]})
    )
    with pytest.raises(K8sError, match="2 deployments"):
        detect_eso(run=runner)


def test_detect_eso_reads_the_version_off_the_operator_container():
    item = deployment(version_label=None, image="registry.example.com/eso:v0.18.1")
    containers = item["spec"]["template"]["spec"]["containers"]
    containers.insert(0, {"name": "sidecar", "image": "registry.example.com/sidecar:v9"})
    containers[1]["name"] = "external-secrets"
    runner = FakeRunner(stdout=json.dumps({"items": [item]}))
    assert detect_eso(run=runner).version == "v0.18.1"


# The subprocess seam ---------------------------------------------------------


def test_run_turns_a_missing_binary_into_a_k8s_error():
    with pytest.raises(K8sError, match="not found on PATH"):
        k8s.run(["definitely-not-a-real-binary-xyz", "version"])


def test_run_turns_a_timeout_into_a_k8s_error(monkeypatch):
    def fake_subprocess_run(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, kwargs.get("timeout", 0))

    monkeypatch.setattr(k8s.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(k8s.subprocess, "run", fake_subprocess_run)
    with pytest.raises(K8sError, match="timed out"):
        k8s.run(["kubectl", "get", "pods"])


# converge_documents ----------------------------------------------------------


def converge(docs, cluster, **kwargs):
    return k8s.converge_documents(docs, run=cluster, **kwargs)


def test_converge_creates_documents_the_cluster_does_not_hold():
    cluster = FakeCluster()
    report = converge(both_docs(), cluster, dry_run=False)
    assert [r.action for r in report.results] == ["create", "create"]
    assert [r.target for r in report.results] == [
        "ServiceAccount/ladon/eso-ns-a-sample",
        "ClusterSecretStore/ladon-ns-a-sample",
    ]


def test_a_second_converge_reports_already_current_and_sends_nothing():
    cluster = FakeCluster()
    converge(both_docs(), cluster, dry_run=False)
    applies = len(cluster.applies)

    report = converge(both_docs(), cluster, dry_run=False)
    assert [r.action for r in report.results] == ["already current", "already current"]
    # The no-op signal is the point: a converged document never reaches kubectl.
    assert len(cluster.applies) == applies


def test_a_document_that_differs_is_an_update():
    stored = store_doc()
    stored["spec"]["provider"]["vault"]["namespace"] = "ns-other"
    cluster = FakeCluster(live=[service_account_doc(), stored])

    report = converge(both_docs(), cluster, dry_run=False)
    assert [r.action for r in report.results] == ["already current", "update"]
    # Only the document that differs was sent.
    assert [d["kind"] for d in yaml.safe_load_all(cluster.streams[-1])] == ["ClusterSecretStore"]


def test_converge_reports_every_document_in_the_order_given():
    cluster = FakeCluster(live=[service_account_doc()])
    report = converge(both_docs(), cluster)
    assert [r.action for r in report.results] == ["already current", "create"]


def test_a_dry_run_converge_writes_nothing():
    cluster = FakeCluster()
    report = converge(both_docs(), cluster, dry_run=True)
    assert [r.action for r in report.results] == ["create", "create"]
    assert report.dry_run is True
    assert "--dry-run=server" in cluster.applies[0]
    assert cluster.objects == {}


def test_converge_verifies_what_it_wrote_and_names_the_field():
    # A cluster that stores something other than what was sent: the read-back is
    # what catches it, and the message names the object and the field, never a
    # value.
    def drop_the_conditions(doc):
        stored = json.loads(json.dumps(doc))
        stored.get("spec", {}).pop("conditions", None)
        return stored

    cluster = FakeCluster(mutate=drop_the_conditions)
    with pytest.raises(K8sError) as excinfo:
        converge([store_doc()], cluster, dry_run=False)
    assert "ClusterSecretStore/ladon-ns-a-sample" in str(excinfo.value)
    assert "spec.conditions" in str(excinfo.value)


def test_a_dry_run_verifies_nothing():
    # Nothing was written, so there is nothing to read back: a dry run against a
    # cluster that would store the document differently is still clean.
    cluster = FakeCluster(mutate=lambda doc: {"kind": doc["kind"], "metadata": doc["metadata"]})
    report = converge([store_doc()], cluster, dry_run=True)
    assert report.results[0].action == "create"


def test_a_blocked_document_is_reported_and_not_verified():
    cluster = FakeCluster(blocked=["ladon-ns-a-sample"])
    report = converge(both_docs(), cluster, dry_run=False)
    assert [r.action for r in report.results] == ["create", "blocked"]
    assert "conflict" in report.results[1].reason
    assert report.blocked == (report.results[1],)


def test_converge_with_no_documents_runs_no_command():
    cluster = FakeCluster()
    assert converge([], cluster, dry_run=False).results == ()
    assert cluster.calls == []


def test_converge_refuses_a_document_it_cannot_place():
    doc = service_account_doc()
    del doc["metadata"]["namespace"]
    with pytest.raises(K8sError, match="cluster-scoped"):
        converge([doc], FakeCluster(), dry_run=False)


def test_a_read_that_fails_for_another_reason_is_an_error():
    # "I could not look" is never "it is not there": that would apply over an
    # object somebody else owns.
    runner = FakeRunner(stderr="Error from server (Forbidden): not allowed\n", returncode=1)
    with pytest.raises(K8sError, match="Forbidden"):
        k8s.converge_documents([service_account_doc()], run=runner, dry_run=False)


def test_a_key_added_inside_a_condition_is_an_update_and_is_re_applied():
    # The scenario the strict comparison exists for: a namespace Ladon never
    # admitted is added beside its selector. Server-side apply treats the
    # condition list as atomic, so re-applying the document removes it.
    tampered = store_doc()
    tampered["spec"]["conditions"][0]["namespaces"] = ["ns-other"]
    cluster = FakeCluster(live=[tampered])

    report = converge([store_doc()], cluster, dry_run=False)
    assert report.results[0].action == "update"
    assert len(cluster.applies) == 1
    assert "namespaces" not in cluster.stored(store_doc())["spec"]["conditions"][0]


def test_a_field_added_outside_the_strict_paths_stays_current():
    live = store_doc()
    live["spec"]["refreshInterval"] = "1h"
    cluster = FakeCluster(live=[live])
    report = converge([store_doc()], cluster, dry_run=False)
    assert report.results[0].action == "already current"
    assert cluster.applies == []


def test_a_failure_after_the_write_carries_the_partial_report():
    # The documents were written; a report that vanished with the exception
    # would leave an operator not knowing what landed.
    cluster = FakeCluster(mutate=lambda doc: {k: v for k, v in doc.items() if k != "spec"})
    with pytest.raises(k8s.ClusterApplyError) as excinfo:
        converge([service_account_doc(), store_doc()], cluster, dry_run=False)
    assert [r.action for r in excinfo.value.report.results] == ["create", "create"]
    assert "spec" in str(excinfo.value)


def test_a_read_back_that_fails_after_the_write_also_carries_the_report():
    calls = {"applied": False}

    def handler(argv, stdin):
        if argv[1] == "apply":
            calls["applied"] = True
            return _Result(argv, stdout="serviceaccount/eso-ns-a-sample created\n")
        if calls["applied"]:
            return _Result(argv, returncode=1, stderr="Error from server (Forbidden): no\n")
        return _Result(argv, returncode=1, stderr='Error from server (NotFound): "x" not found\n')

    with pytest.raises(k8s.ClusterApplyError) as excinfo:
        k8s.converge_documents(
            [service_account_doc()], run=FakeRunner(handler=handler), dry_run=False
        )
    assert [r.action for r in excinfo.value.report.results] == ["create"]
