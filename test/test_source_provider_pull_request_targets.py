from __future__ import annotations

import pytest

from kiro_crew.monitoring.targets import (
    infer_pull_request_kind,
    parse_azure_devops_pull_request_target,
    parse_bitbucket_pull_request_target,
    parse_gitlab_merge_request_target,
)


@pytest.mark.parametrize(
    ("url", "kind"),
    [
        ("https://github.com/acme/widgets/pull/7", "github_pull_request"),
        ("https://gitlab.com/acme/widgets/-/merge_requests/8", "gitlab_merge_request"),
        (
            "https://dev.azure.com/acme/project/_git/widgets/pullrequest/9",
            "azure_devops_pull_request",
        ),
        ("https://bitbucket.org/acme/widgets/pull-requests/10", "bitbucket_pull_request"),
    ],
)
def test_infer_pull_request_kind_accepts_only_supported_canonical_hosts(url, kind):
    assert infer_pull_request_kind(url, gitlab_hosts=[]) == kind


def test_gitlab_parser_accepts_configured_exact_self_managed_host():
    target = parse_gitlab_merge_request_target(
        "https://git.example.com/group/sub/repo/-/merge_requests/12",
        gitlab_hosts=["git.example.com"],
    )

    assert target.host == "git.example.com"
    assert target.project_path == "group/sub/repo"
    assert target.iid == 12


def test_gitlab_parser_accepts_only_an_explicit_self_managed_port():
    target = parse_gitlab_merge_request_target(
        "https://git.example.com:8443/group/repo/-/merge_requests/12",
        gitlab_hosts=["git.example.com:8443"],
    )
    assert target.host == "git.example.com:8443"

    with pytest.raises(ValueError):
        parse_gitlab_merge_request_target(
            "https://git.example.com:9443/group/repo/-/merge_requests/12",
            gitlab_hosts=["git.example.com:8443"],
        )


@pytest.mark.parametrize(
    "url",
    [
        "http://gitlab.com/acme/repo/-/merge_requests/1",
        "https://evil.example/acme/repo/-/merge_requests/1",
        "https://gitlab.com/acme/repo/-/merge_requests/0",
        "https://gitlab.com/acme/repo/-/merge_requests/1?token=secret",
    ],
)
def test_gitlab_parser_rejects_noncanonical_or_unallowed_targets(url):
    with pytest.raises(ValueError):
        parse_gitlab_merge_request_target(url, gitlab_hosts=[])


def test_azure_parser_projects_exact_identity():
    target = parse_azure_devops_pull_request_target(
        "https://dev.azure.com/acme/project/_git/widgets/pullrequest/9"
    )

    assert target.identity == "dev.azure.com/acme/project/widgets#9"
    assert target.url == "https://dev.azure.com/acme/project/_git/widgets/pullrequest/9"


def test_azure_parser_accepts_canonical_encoded_spaces_in_project_and_repository():
    target = parse_azure_devops_pull_request_target(
        "https://dev.azure.com/acme/My%20Project/_git/Widget%20Repo/pullrequest/9"
    )

    assert target.project == "My Project"
    assert target.repository == "Widget Repo"
    assert target.url == (
        "https://dev.azure.com/acme/My%20Project/_git/Widget%20Repo/pullrequest/9"
    )


def test_bitbucket_parser_projects_exact_identity():
    target = parse_bitbucket_pull_request_target(
        "https://bitbucket.org/acme/widgets/pull-requests/10"
    )

    assert target.identity == "bitbucket.org/acme/widgets#10"
    assert target.url == "https://bitbucket.org/acme/widgets/pull-requests/10"


@pytest.mark.parametrize(
    ("parser", "url"),
    [
        (
            parse_azure_devops_pull_request_target,
            "https://acme.visualstudio.com/project/_git/widgets/pullrequest/9",
        ),
        (
            parse_azure_devops_pull_request_target,
            "https://dev.azure.com/acme/project/_git/widgets/pullrequest/9/extra",
        ),
        (
            parse_bitbucket_pull_request_target,
            "https://bitbucket.example/acme/widgets/pull-requests/10",
        ),
        (
            parse_bitbucket_pull_request_target,
            "https://bitbucket.org/acme/widgets/pull-requests/10?x=1",
        ),
    ],
)
def test_cloud_only_parsers_reject_other_hosts_and_extra_url_data(parser, url):
    with pytest.raises(ValueError):
        parser(url)
