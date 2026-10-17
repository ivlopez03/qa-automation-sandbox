"""UI tests with Playwright. Locate by data-testid so 'random element IDs' mode doesn't break them."""
import re

import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.ui


def login(page: Page, app_url: str, username: str, password: str, wait: bool = True):
    page.goto(f"{app_url}/login")
    page.get_by_test_id("login-username").fill(username)
    page.get_by_test_id("login-password").fill(password)
    page.get_by_test_id("login-submit").click()
    if wait:
        expect(page).to_have_url(re.compile(r"/dashboard$"))


def test_admin_dashboard_kpis_match_api(page: Page, app_url, admin_api):
    login(page, app_url, "admin", "Admin123!")
    expect(page.get_by_test_id("kpi-strip")).to_have_attribute("data-loaded", "true", timeout=15_000)
    total = admin_api.get("/api/v1/reports/summary").json()["total_leads"]
    expect(page.get_by_test_id("kpi-total-leads")).to_have_text(f"{total:,}")


def test_bad_password_shows_error(page: Page, app_url):
    login(page, app_url, "admin", "nope", wait=False)
    expect(page.get_by_test_id("login-error")).to_have_text("Invalid username or password")
    expect(page).to_have_url(re.compile(r"/login"))


def test_agent_has_no_admin_navigation(page: Page, app_url):
    login(page, app_url, "alice", "User123!")
    expect(page.get_by_test_id("current-role")).to_have_text("user")
    expect(page.get_by_test_id("nav-users")).to_have_count(0)
    page.goto(f"{app_url}/admin/users")
    expect(page.get_by_test_id("error-code")).to_have_text("Not allowed")


def test_create_and_progress_lead(page: Page, app_url, unique_email):
    email = unique_email("ui")
    login(page, app_url, "alice", "User123!")
    page.get_by_test_id("new-lead-button").click()
    page.get_by_test_id("input-first-name").fill("  maria ")
    page.get_by_test_id("input-email").fill(email.upper())
    page.get_by_test_id("input-phone").fill("602.555.0123")
    page.get_by_test_id("input-source").select_option("referral")
    page.get_by_test_id("save-lead").click()

    expect(page.get_by_test_id("lead-name")).to_have_text("Maria")
    expect(page.get_by_test_id("fact-email")).to_have_text(email)
    expect(page.get_by_test_id("fact-phone")).to_have_text("+16025550123")
    expect(page.get_by_test_id("toast")).to_have_text("Lead added")

    page.get_by_test_id("transition-select").select_option("contacted")
    page.get_by_test_id("transition-submit").click()       # auto-waits while ui_delay keeps it disabled
    expect(page.get_by_test_id("lead-status")).to_have_text("contacted")
    expect(page.get_by_test_id("timeline-event")).to_have_count(2)


def test_validation_error_is_shown(page: Page, app_url):
    login(page, app_url, "admin", "Admin123!")
    page.goto(f"{app_url}/leads/new")
    page.get_by_test_id("input-first-name").fill("x")
    page.get_by_test_id("input-email").fill("x@y.co")
    page.get_by_test_id("input-phone").fill("123")
    page.get_by_test_id("input-source").select_option("web")
    page.get_by_test_id("save-lead").click()
    expect(page.get_by_test_id("form-error")).to_contain_text("phone must contain 10 digits")
