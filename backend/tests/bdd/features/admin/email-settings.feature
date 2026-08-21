Feature: Admin Email Settings
  As an organisation admin
  I want to configure SMTP email settings including a configurable timeout
  So that transactional emails are sent through the right relay within a bounded time

  Scenario: Admin can view email settings
    Given I am authenticated as a system admin
    And the organisation has no saved email settings
    When I GET the email settings for the test organisation
    Then the response status is 200
    And the email settings response has masked password and default timeout 30

  Scenario: Admin can update email settings including the SMTP timeout
    Given I am authenticated as a system admin
    And the organisation has no saved email settings
    When I PUT the email settings for the test organisation with a timeout of 10
    Then the response status is 200
    And the email settings response includes a timeout of 10

  Scenario: SMTP timeout below 1 second is rejected
    Given I am authenticated as a system admin
    When I PUT the email settings for the test organisation with a timeout of 0
    Then the response status is 422

  Scenario: SMTP timeout above 120 seconds is rejected
    Given I am authenticated as a system admin
    When I PUT the email settings for the test organisation with a timeout of 121
    Then the response status is 422

  Scenario: SMTP password longer than 256 characters is rejected
    Given I am authenticated as a system admin
    When I PUT the email settings for the test organisation with a password of 300 characters
    Then the response status is 422

  Scenario: Admin can test email settings when SMTP is configured
    Given I am authenticated as a system admin
    And email settings are configured with SMTP host "smtp.example.com"
    And the SMTP relay accepts the test email
    When I POST a test email to "admin@example.com" for the test organisation
    Then the response status is 200
    And the response confirms the test email was sent

  Scenario: Test email reports an SMTP failure without leaking internals
    Given I am authenticated as a system admin
    And email settings are configured with SMTP host "smtp.example.com"
    And the SMTP relay rejects the test email with "Connection refused"
    When I POST a test email to "admin@example.com" for the test organisation
    Then the response status is 200
    And the response reports the test email failed

  Scenario: Malformed test-send recipient is rejected
    Given I am authenticated as a system admin
    And email settings are configured with SMTP host "smtp.example.com"
    And the SMTP relay accepts the test email
    When I POST a test email to "not-an-email" for the test organisation
    Then the response status is 422
    And no test email is sent to the SMTP relay

  Scenario: Test-send recipient header injection is rejected
    Given I am authenticated as a system admin
    And email settings are configured with SMTP host "smtp.example.com"
    And the SMTP relay accepts the test email
    When I POST a test email with header injection for the test organisation
    Then the response status is 422
    And no test email is sent to the SMTP relay

  Scenario: Test-send endpoint is rate limited per organisation
    Given I am authenticated as a system admin
    And email settings are configured with SMTP host "smtp.example.com"
    And the SMTP relay accepts the test email
    And the test-send rate limit budget is exhausted for the test organisation
    When I POST a test email to "admin@example.com" for the test organisation
    Then the response status is 429
    And the response carries a Retry-After header
    And no test email is sent to the SMTP relay

  Scenario: Viewer cannot read email settings
    Given I am authenticated as a viewer in org "default"
    When I GET the email settings for the test organisation
    Then the response status is 403
