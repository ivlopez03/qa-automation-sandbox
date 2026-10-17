# Leadflow: Product Requirements, Business Rules and Acceptance Criteria

Oct 17, 2026 · @Ivan Lopez

## Overview

Leadflow takes leads from web forms, CSV files and a vendor feed, cleans them, hands each one to the least-busy agent, and tracks it until it converts into a deal or is lost. Admins see the whole pipeline; agents see only their own leads.

**Goal:** no lead is lost or counted twice between import and revenue reporting, and every report number can be traced to rows in the database.

| Persona | Account | Needs |
| --- | --- | --- |
| Admin (sales ops) | `admin` | import leads, watch processing, manage users, see team-wide reports |
| Agent | `alice`, `bob`, `carol` | add leads, work their own pipeline, see their own numbers |
| Deactivated agent | `dave` | must not be able to sign in |

**In scope:** sign-in and roles, lead entry, import and processing, the sales lifecycle, reports, user admin, test conditions.

**Out of scope:** email or SMS outreach, password reset, multi-tenant accounts, editing a lead's email or source by agents.

Ids used below: **BR** = business rule, **FR** = functional requirement, **AC** = acceptance criterion, **NFR** = non-functional requirement. Tests should name the ids they cover.

## Business rules

These are the test oracle: calculate expected values from them, never from what the app returns.

**Lead data**

| ID | Rule | Example |
| --- | --- | --- |
| BR-01 | `first_name` is required, trimmed, Title Case, at most 50 characters. `last_name` is optional, trimmed, Title Case. | `"  maria "` → `Maria` |
| BR-02 | `email` is required, trimmed, lowercased, and must look like `name@domain.tld`. | `" Maria@Initech.COM "` → `maria@initech.com` |
| BR-03 | `email` is unique across all leads, compared after normalizing. | `A@x.com` and `a@x.com` are the same lead |
| BR-04 | `phone` is optional. Non-digits are removed; 11 digits starting with 1 drop the 1; exactly 10 digits must remain; stored as `+1` + 10 digits. | `1 (602) 555-0142` → `+16025550142`; `555-12` is invalid |
| BR-05 | `company` is trimmed; empty becomes null. | `"  "` → null |
| BR-06 | `source` is lowercased and must be one of: web, referral, ads, partner, event. | `fax` is rejected |
| BR-07 | When a record breaks several rules, every broken rule is reported, not only the first. | `email is invalid; phone must contain 10 digits` |

**Scoring and assignment**

| ID | Rule | Example |
| --- | --- | --- |
| BR-10 | Score = source base + 20 if company + 10 if phone + 20 if the email domain is not free, capped at 100. | partner (30) + company + phone + business email = 80 |
| BR-11 | Base scores: referral 40, partner 30, web 20, event 15, ads 10. |  |
| BR-12 | Free domains: gmail.com, yahoo.com, hotmail.com, outlook.com, aol.com. |  |
| BR-13 | Estimated value = score × 100, in USD. | score 80 → $8,000.00 |
| BR-14 | A lead an agent creates is owned by that agent. | alice adds a lead → owner alice |
| BR-15 | Imported and admin-created leads go to the active agent with the fewest open leads (new, contacted, qualified); ties go to the lowest user id. No active agent → unassigned. |  |
| BR-16 | Score and estimated value are recalculated when phone or company change. |  |

**Import and processing**

| ID | Rule |
| --- | --- |
| BR-20 | Every received record is stored unchanged as `pending` before any validation. |
| BR-21 | Processing handles pending records oldest first. Each ends as exactly one of: `processed` (lead created and linked), `rejected` (rule broken, reasons saved) or `duplicate` (email already exists). |
| BR-22 | A duplicate inside the same batch is caught: the first copy loads, later copies become `duplicate`. |
| BR-23 | Each record is saved on its own, so a crash leaves earlier records loaded and later ones `pending`. The next run finishes them. Nothing is loaded twice. |
| BR-24 | Only one processing run at a time. |
| BR-25 | A repeated import with the same `Idempotency-Key` returns the first response and stores nothing new. |
| BR-26 | One import holds 1 to 1000 records. A CSV must include `first_name`, `email` and `source` columns. |

**Lifecycle**

| ID | Rule |
| --- | --- |
| BR-30 | Allowed moves: new → contacted or lost; contacted → qualified or lost; qualified → converted or lost. |
| BR-31 | `converted` and `lost` are final. Any move not in BR-30 is refused and changes nothing. |
| BR-32 | Converting requires an amount greater than 0 and creates exactly one deal, rounded to 2 decimals, owned by the lead's owner. |
| BR-33 | Every create, move and reassignment adds a history entry with the actor; every write adds an audit entry. |

**Reports**

| ID | Rule |
| --- | --- |
| BR-40 | Conversion rate = converted ÷ total leads × 100, rounded to 2 decimals; 0 when there are no leads. |
| BR-41 | Average deal = total revenue ÷ number of deals, rounded to 2 decimals; 0 when there are no deals. |
| BR-42 | Open pipeline value = sum of estimated value of new, contacted and qualified leads. |
| BR-43 | Per source: cost = leads × cost per lead; ROI % = (revenue − cost) ÷ cost × 100, empty when cost is 0. Cost per lead: web $4.50, referral $0, ads $12.75, partner $8.00, event $20.00. |
| BR-44 | Agents' reports cover only their own leads. Daily imports and reconciliation are admin-only. |
| BR-45 | CSV and JSON versions of a report contain the same rows and values. |

**Access**

| ID | Rule |
| --- | --- |
| BR-50 | Two roles: admin (everything) and user (agent: own leads only). |
| BR-51 | An agent asking for another agent's lead gets “not found”, so other leads' ids are not disclosed. |
| BR-52 | Only admins import, process, reassign, delete, manage users and change test conditions. |
| BR-53 | Deactivated users cannot sign in, and their existing sessions stop working. |
| BR-54 | After 10 failed sign-ins in a minute from one address, further attempts are refused until the minute passes. |
| BR-55 | An admin cannot deactivate or demote their own account. |
| BR-56 | Users cannot change their own role. |

## Functional requirements

Each story is available in the UI and through the REST API (`/api/v1`, documented at `/docs`), unless it says API only.

| ID | Area | User story | Rules |
| --- | --- | --- | --- |
| FR-01 | Sign-in | As any user, I sign in with username and password and land on my dashboard. | BR-53, BR-54 |
| FR-02 | Sign-in | As any user, I sign out and can no longer reach protected pages. |  |
| FR-03 | Sign-in | As a user, I see only the navigation my role allows. | BR-50, BR-52 |
| FR-10 | Leads | As an agent or admin, I add a lead by hand and see it cleaned, scored and assigned. | BR-01–BR-16 |
| FR-11 | Leads | As an agent or admin, I list leads with search, status and source filters, sorting and pagination. | BR-44, BR-51 |
| FR-12 | Leads | As an agent or admin, I open a lead and see its details, deal and history. | BR-33, BR-51 |
| FR-13 | Leads | As an agent or admin, I edit a lead's name, phone or company. | BR-01, BR-04, BR-05, BR-16 |
| FR-14 | Leads | As an admin, I replace all of a lead's fields, including email and source (API only). | BR-02, BR-03, BR-06 |
| FR-15 | Leads | As an admin, I reassign a lead to another active agent or leave it unassigned. | BR-15, BR-33, BR-52 |
| FR-16 | Leads | As an admin, I delete a lead together with its history and deal. | BR-52 |
| FR-20 | Import | As an admin, I import leads as pasted JSON. | BR-20, BR-25, BR-26 |
| FR-21 | Import | As an admin, I import leads from a CSV file. | BR-20, BR-26 |
| FR-22 | Import | As an admin, I pull a batch from the vendor feed. | BR-20 |
| FR-23 | Import | As an admin, I start processing and watch its progress until it finishes. | BR-21–BR-24 |
| FR-24 | Import | As an admin, I see rejected and duplicate records with their reasons. | BR-07, BR-21 |
| FR-30 | Lifecycle | As an agent or admin, I move a lead to the next allowed status, with an optional note. | BR-30, BR-31, BR-33 |
| FR-31 | Lifecycle | As an agent or admin, I convert a qualified lead by entering a deal amount. | BR-32 |
| FR-40 | Reports | As any user, I see the summary: totals by status, conversion rate, revenue, average deal, open pipeline. | BR-40–BR-42, BR-44 |
| FR-41 | Reports | As any user, I see results by source and by agent, and download them as CSV. | BR-43–BR-45 |
| FR-42 | Reports | As an admin, I see daily import counts and a reconciliation of imported vs loaded records. | BR-21, BR-44 |
| FR-50 | Admin | As an admin, I add users and deactivate, reactivate or change the role of others. | BR-53, BR-55 |
| FR-51 | Admin | As an admin, I switch test conditions on and off (API delay and errors, slow pages, random element ids, slow or crashing processing). |  |
| FR-52 | Admin | As an admin, I reset all data to the same starting state. |  |
| FR-53 | Account | As any user, I change my own name or password (API only). | BR-56 |

## Acceptance criteria

Written in Gherkin so they can go straight into `.feature` files (pytest-bdd) or become test names. Tags link each scenario to its requirement. Every scenario starts from freshly reset data unless it says otherwise.

**Sign-in and roles**

```gherkin
Feature: Sign-in and roles

  @AC-01 @FR-01
  Scenario: Successful sign-in
    Given I am on the sign-in page
    When I sign in as "admin" with "Admin123!"
    Then I am on the dashboard
    And the sidebar shows "Ada Admin" with role "admin"

  @AC-02 @FR-01
  Scenario: Wrong password
    When I sign in as "admin" with "wrong"
    Then I stay on the sign-in page
    And I see "Invalid username or password"
    And a "login_failed" entry is added to the audit log

  @AC-03 @FR-01 @BR-53
  Scenario: Deactivated user
    When I sign in as "dave" with "User123!"
    Then the API answers 403 "User is deactivated"

  @AC-04 @FR-01 @BR-54
  Scenario: Too many failed sign-ins
    Given 10 failed sign-ins from my address in the last minute
    When I try to sign in again, even with the right password
    Then the API answers 429 with a Retry-After header

  @AC-05 @FR-02
  Scenario: Sign out
    Given I am signed in
    When I sign out and open "/dashboard"
    Then I am sent to the sign-in page

  @AC-06 @FR-03 @BR-52
  Scenario: Agent cannot reach admin pages
    Given I am signed in as "alice"
    Then the sidebar has no "Users", "Import leads" or "Test conditions" links
    When I open "/admin/users"
    Then I see "Not allowed"

  @AC-07 @BR-53
  Scenario: Deactivation ends an existing session
    Given "bob" is signed in
    When an admin deactivates "bob"
    Then bob's next API call answers 401
```

**Leads**

```gherkin
Feature: Leads

  @AC-10 @FR-10 @BR-01 @BR-02 @BR-04 @BR-10 @BR-14
  Scenario: Agent adds a lead and it is cleaned, scored and owned
    Given I am signed in as "alice"
    When I add a lead with first name "  jane ", email " JANE@ACMELEGAL.COM ",
      phone "1 (602) 555-0199", company "Acme Legal", source "partner"
    Then the lead page shows name "Jane", email "jane@acmelegal.com", phone "+16025550199"
    And the score is 80 and the estimated value is "$8000.00"
    And the owner is "alice" and the status is "new"
    And I see the message "Lead added"

  @AC-11 @FR-10 @BR-07
  Scenario Outline: Invalid lead is refused with a reason
    When I add a lead with <field> set to <value>
    Then the lead is not saved
    And I see "<message>"
    Examples:
      | field      | value          | message                         |
      | email      | not-an-email   | email is invalid                |
      | phone      | 555-12         | phone must contain 10 digits    |
      | source     | carrier-pigeon | unknown source 'carrier-pigeon' |
      | first_name | (spaces only)  | first_name is required          |

  @AC-12 @FR-10 @BR-03
  Scenario: Duplicate email
    Given a lead with email "a@x.com" exists
    When I add a lead with email "A@X.COM"
    Then the API answers 409 and no lead is created

  @AC-13 @FR-10 @BR-15
  Scenario: Admin-created lead goes to the least-busy agent
    Given the active agent with the fewest open leads is "carol"
    When an admin adds a lead
    Then its owner is "carol"

  @AC-14 @FR-11 @BR-44
  Scenario: Agent sees only own leads
    Given I am signed in as "alice"
    When I list leads
    Then every lead shown has owner "alice"
    And the total equals alice's lead count in the database

  @AC-15 @FR-11
  Scenario: Filtering, sorting and paging
    Given I am signed in as "admin"
    When I filter by status "qualified" and sort by highest score
    Then every row has status "qualified" and scores never increase down the page
    And pages 1 and 2 share no lead

  @AC-16 @FR-12 @BR-51
  Scenario: Another agent's lead is hidden
    Given lead 7 belongs to "alice"
    When "bob" opens lead 7
    Then he sees "not found" (404), not "forbidden"

  @AC-17 @FR-13 @BR-16
  Scenario: Editing recalculates the score
    Given a web lead with a business email, no company and no phone (score 40)
    When I add company "Initech"
    Then the score is 60 and the estimated value is 6000

  @AC-18 @FR-15 @BR-52
  Scenario: Only admins reassign
    When "alice" tries to set a lead's owner
    Then the API answers 403
    When an admin reassigns the lead to "bob"
    Then the owner is "bob" and the history shows a "reassigned" entry

  @AC-19 @FR-16
  Scenario: Admin deletes a lead
    When an admin deletes a lead
    Then the API answers 204
    And the lead, its history and its deal are gone
```

**Import and processing**

```gherkin
Feature: Import and processing

  @AC-20 @FR-20 @BR-20
  Scenario: Imported records wait as pending, untouched
    When an admin imports 3 records as JSON
    Then the response says 3 received with a batch id
    And 3 raw records with that batch id are "pending"
    And each stored payload equals what was sent

  @AC-21 @FR-23 @BR-21 @BR-22
  Scenario: Every record ends in exactly one outcome
    Given a pending batch of:
      | record                       | expected outcome |
      | valid, business email        | processed        |
      | valid, gmail address         | processed        |
      | email "no-at-sign.com"       | rejected         |
      | source "fax"                 | rejected         |
      | same email as the first, caps| duplicate        |
    When an admin runs processing and waits for it to finish
    Then the run status is "succeeded"
    And each record has its expected outcome
    And rejected records name the broken rule
    And each processed record links to exactly one new lead

  @AC-22 @FR-23 @BR-24
  Scenario: One run at a time
    Given a processing run is in progress
    When an admin starts another run
    Then the API answers 409

  @AC-23 @FR-23 @BR-23
  Scenario: Recovery after a crash
    Given processing is set to crash 10% of the time
    And 60 records are pending
    When an admin re-runs processing until a run succeeds
    Then no record is still pending
    And no lead was created twice
    And the reconciliation report passes every check

  @AC-24 @FR-20 @BR-25
  Scenario: Repeated import with the same key
    When an admin imports the same records twice with Idempotency-Key "k1"
    Then both responses have the same batch id
    And the second has header "Idempotent-Replay: true"
    And only one set of raw records exists

  @AC-25 @FR-21 @BR-26
  Scenario Outline: Bad import requests
    When an admin sends <request>
    Then the API answers <status>
    Examples:
      | request                          | status |
      | a body that is not JSON          | 400    |
      | an empty array                   | 422    |
      | 1001 records                     | 422    |
      | a .txt file to the CSV import    | 415    |
      | a CSV with no email column       | 422    |
```

**Lifecycle**

```gherkin
Feature: Lead lifecycle

  @AC-30 @FR-30 @BR-30 @BR-31
  Scenario Outline: Allowed and refused moves
    Given a lead in status "<from>"
    When I move it to "<to>"
    Then the API answers <status>
    Examples:
      | from      | to        | status |
      | new       | contacted | 200    |
      | new       | qualified | 409    |
      | contacted | qualified | 200    |
      | qualified | lost      | 200    |
      | converted | lost      | 409    |
      | lost      | new       | 409    |

  @AC-31 @FR-31 @BR-32
  Scenario: Converting needs an amount
    Given a qualified lead
    When I convert it without an amount
    Then the API answers 422 and the status stays "qualified"
    When I convert it with amount 1234.567
    Then the status is "converted"
    And exactly one deal of 1234.57 exists for the lead

  @AC-32 @FR-30 @BR-33
  Scenario: History records each move
    Given a new lead
    When I move it to contacted, then qualified
    Then its history lists created, contacted, qualified in that order with my username

  @AC-33 @FR-31
  Scenario: Amount field only for converting (UI)
    Given I open a qualified lead
    When I choose "lost"
    Then the deal amount field is hidden
    When I choose "converted"
    Then the deal amount field is shown
```

**Reports**

```gherkin
Feature: Reports

  @AC-40 @FR-40 @BR-40 @BR-41 @BR-42
  Scenario: Summary matches the database
    When an admin opens the summary report
    Then total leads, counts by status, revenue, conversion rate, average deal
      and open pipeline value each equal the same figure calculated with SQL

  @AC-41 @FR-41 @BR-43
  Scenario: By-source rolls up to the summary
    Then the sum of leads, converted and revenue across sources equals the summary
    And each source's conversion rate, cost and ROI follow BR-40 and BR-43

  @AC-42 @FR-41 @BR-45
  Scenario: CSV equals JSON
    When I download the by-agent report as CSV and as JSON
    Then they have the same number of rows and the same values

  @AC-43 @FR-40 @BR-44
  Scenario: Agent numbers are scoped
    When "alice" opens the summary
    Then total leads equals the number of leads owned by alice

  @AC-44 @FR-42
  Scenario: Reconciliation
    When an admin opens the reconciliation report
    Then every check passes
```

**Admin**

```gherkin
Feature: Administration

  @AC-50 @FR-50
  Scenario: Add a user
    When an admin adds user "erin" with role "user" and an 8+ character password
    Then "erin" can sign in and is offered new leads by BR-15

  @AC-51 @FR-50 @BR-55
  Scenario: Admin cannot lock themselves out
    When the admin tries to deactivate or demote their own account
    Then the API answers 409

  @AC-52 @FR-53 @BR-56
  Scenario: No self-promotion
    When "alice" updates her own profile with role "admin"
    Then the API answers 422 and her role is still "user"

  @AC-53 @FR-51
  Scenario: Test conditions are validated
    When an admin sets the API failure rate to 2
    Then the API answers 422 "chaos_error_rate must be between 0.0 and 1.0"

  @AC-54 @FR-52
  Scenario: Reset gives the same data every time
    When an admin resets the data twice with seed 42
    Then both resets report the same counts of users, raw records, leads and deals
```

## Non-functional requirements

Performance figures are proposed targets for a single local instance; no baseline has been measured yet (see open questions).

| ID | Area | Requirement | How to verify |
| --- | --- | --- | --- |
| NFR-01 | Performance | With 20 concurrent users and 300 seeded leads, p95 response under 500 ms for lead list, lead detail, create lead and summary report. | JMeter plan `perf/leadflow_load.jmx`, `make perf THREADS=20` |
| NFR-02 | Performance | With 20,000 seeded leads, the same calls stay under 2 s p95; the full CSV export finishes under 10 s. | `POST /admin/reset?leads=20000`, then JMeter |
| NFR-03 | Performance | Error rate under 1% during a 5-minute run with test conditions off. | JMeter dashboard |
| NFR-10 | Reliability | A crashed processing run never loses or duplicates a record (BR-23). | AC-23 |
| NFR-11 | Reliability | Reports never disagree with each other or with the database. | AC-40 to AC-44 |
| NFR-12 | Reliability | Under API delay or 503s, the UI shows a clear message instead of breaking, and a retried call succeeds. | turn on test conditions, run UI suite |
| NFR-20 | Security | Passwords are stored hashed; sign-in tokens are signed and expire after 1 hour. | DB check; tampered or expired token → 401 |
| NFR-21 | Security | No SQL injection, IDOR, reflected XSS or mass assignment with vulnerable mode off. | security tests pass in normal mode, fail in `SANDBOX_VULNS=1` |
| NFR-22 | Security | Errors return a short message and a request id, never a stack trace. | force a 500, inspect body |
| NFR-23 | Security | Sign-in redirects only to pages on the same site. | `/login?next=//evil.com` lands on the dashboard |
| NFR-30 | Testability | Every interactive element and data cell has a stable `data-testid`. | UI suite passes with random element ids on |
| NFR-31 | Testability | Every response carries `X-Request-ID`; a supplied id is echoed back. | API check |
| NFR-32 | Testability | Reset with the same seed always produces the same data. | AC-54 |
| NFR-40 | Usability | Pages work at 360 px wide and in light and dark mode; keyboard focus is visible. | Playwright viewport and color-scheme runs |

## Traceability

Of 39 criteria, the example suite fully covers 12, partly covers 15 and misses 12. The gaps are the backlog for the automation work; update the Coverage column as tests land.

| Criterion | Level | Existing test (`tests/`) | Coverage | Still missing |
| --- | --- | --- | --- | --- |
| AC-01 | API, UI | `test_login_returns_token_and_user`, `test_admin_dashboard_kpis_match_api` | Covered |  |
| AC-02 | API, UI | `test_bad_password_shows_error` | Partial | audit-log entry |
| AC-03 | API | `test_login_rejections[dave]` | Covered |  |
| AC-04 | API | `test_failed_logins_are_rate_limited` | Partial | right password still refused; Retry-After header |
| AC-05 | UI | none | Gap | whole scenario |
| AC-06 | UI | `test_agent_has_no_admin_navigation` | Partial | Import and Test conditions links |
| AC-07 | API | none | Gap | whole scenario |
| AC-10 | API, UI | `test_create_lead_normalizes_scores_and_assigns`, `test_create_and_progress_lead` | Covered |  |
| AC-11 | API, UI | `test_create_lead_validation`, `test_validation_error_is_shown` | Covered |  |
| AC-12 | API | `test_duplicate_email_conflicts` | Covered |  |
| AC-13 | API, data | none | Gap | expected owner calculated with SQL |
| AC-14 | API, data | `test_user_sees_only_own_leads` | Partial | total compared with the database |
| AC-15 | API, UI | `test_pagination_is_consistent`, `test_sort_by_score_desc` | Partial | status filter; UI filters |
| AC-16 | API | `test_user_sees_only_own_leads` | Covered |  |
| AC-17 | API | none | Gap | whole scenario |
| AC-18 | API | `test_user_cannot_reassign` | Partial | admin reassign and history entry |
| AC-19 | API, data | `test_admin_deletes_lead` | Partial | history and deal removed |
| AC-20 | API, data | `test_etl_batch_outcomes` (payload check) | Partial | pending count per batch |
| AC-21 | data | `test_etl_batch_outcomes` | Covered |  |
| AC-22 | API | `test_only_one_etl_job_at_a_time` | Covered |  |
| AC-23 | data | none | Gap | whole scenario |
| AC-24 | API | `test_ingest_is_idempotent` | Partial | raw row count unchanged |
| AC-25 | API | none | Gap | whole scenario |
| AC-30 | API | `test_full_lifecycle_to_converted`, `test_cannot_skip_pipeline_stages` | Partial | full 25-pair matrix |
| AC-31 | API, data | `test_full_lifecycle_to_converted` | Partial | deal row and rounding in the database |
| AC-32 | API | `test_full_lifecycle_to_converted` | Partial | actor username |
| AC-33 | UI | none | Gap | whole scenario |
| AC-40 | data | `test_summary_matches_database` | Partial | average deal, open pipeline |
| AC-41 | data | `test_by_source_rolls_up_to_summary` | Partial | cost and ROI |
| AC-42 | data | `test_csv_export_matches_json` | Covered |  |
| AC-43 | data | `test_user_report_is_scoped` | Covered |  |
| AC-44 | data | `test_reconciliation_checks_pass` | Covered |  |
| AC-50 | API, UI | none | Gap | whole scenario |
| AC-51 | API | none | Gap | whole scenario |
| AC-52 | API | `test_me_patch_rejects_role_escalation` | Covered |  |
| AC-53 | API | none | Gap | whole scenario |
| AC-54 | API, data | none | Gap | whole scenario |
| NFR-01 to 03 | performance | `perf/leadflow_load.jmx` | Partial | targets as JMeter assertions |
| NFR-21 | security | none | Gap | one regression test per flaw |

## Open questions

- [ ] Performance targets in NFR-01 to NFR-03 are proposals. Run a baseline at 300 and 20,000 leads and confirm or adjust them.
- [ ] Should a deactivated agent's open leads be reassigned automatically? Today they stay with that agent.
- [ ] Should agents be allowed to mark a lead lost straight from new, or must it be contacted first? Today new → lost is allowed (BR-30).
- [ ] Is the conversion rate meant over all leads, or only over closed leads (converted + lost)? Today it uses all leads (BR-40).
- [ ] Should cookie-based actions in the UI require a CSRF token? Today they do not.
- [ ] How long should rejected raw records be kept before cleanup? Today they are kept forever.
