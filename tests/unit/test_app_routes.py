from midware.app import create_app


def test_dashboard_page_is_registered_by_application_factory():
    paths = create_app().openapi()["paths"]

    assert "/dashboard" in paths
