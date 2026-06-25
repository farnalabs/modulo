Feature: Jordan — Community Contributor / Library Author
  As Jordan, an open-source contributor
  I want to build, share, and fork SDLC modules in the community library
  So that I can automate my OSS workflow and help others do the same

  @goal-jordan-browse-library
  Scenario: Jordan browses the community library
    Given the community library contains SDLC modules
    When I browse the library
    Then I see primitives organised by type: schemas, agents, workflows, integrations
    And I can filter by category and sort by downloads or rating

  @goal-jordan-fork-workflow
  Scenario: Jordan forks a community workflow for his OSS project
    Given the community library has workflow "issue-to-pr"
    When I copy the workflow to my workspace
    Then a local copy is created with forked_from set to the community source
    And I can edit the agent prompts for my project conventions

  @goal-jordan-contribute-primitive @awaiting-implementation
  Scenario: Jordan contributes a new agent to the community library
    Given I have built a "release-notes" agent with schema and prompts
    When I submit the agent as a library contribution
    Then the contribution is accepted for automated quality checks
    And automated checks run: schema validity, prompt safety, eval coverage
    And if all checks pass, the contribution enters the review queue

  @goal-jordan-contribution-provenance @awaiting-implementation
  Scenario: Jordan's contribution carries versioned provenance
    Given I submitted "release-notes" agent version 1.0.0
    When another user views the agent in the library
    Then they see the author, version, and changelog
    And they see which prompt and schema versions the agent depends on

  @goal-jordan-ratings
  Scenario: Jordan sees download counts and ratings for his contribution
    Given my "release-notes" agent has been published for 30 days
    When I view my contribution profile
    Then I see 47 downloads and 4.2 average rating
    And I see user reviews with comments

  @goal-jordan-contribution-update @awaiting-implementation
  Scenario: Jordan updates his contributed primitive with improvements
    Given my "release-notes" agent is published at version 1.0.0
    When I submit version 1.1.0 with improved prompts
    Then the new version is available in the library
    And existing users see an update notification

  @goal-jordan-eval-packaging @awaiting-implementation
  Scenario: Jordan packages evals alongside his contributed agent
    Given I want contributors to verify my agent's quality
    When I include an eval suite in my library contribution
    Then the eval suite is published alongside the agent
    And users can run the evals to validate outputs

  @goal-jordan-export-portable
  Scenario: Jordan exports his pipeline to share with contributors
    Given I have a pipeline configured for my OSS project
    When I export it as a YAML bundle
    Then the bundle includes node topology, schemas, and prompts
    And the bundle contains no secrets or credentials

  @goal-jordan-import-community
  Scenario: Jordan imports a contributor's pipeline
    Given another contributor shared a pipeline YAML bundle
    When I import the bundle
    Then a new pipeline is created with the same topology
    And I can resolve any schema name conflicts

  @goal-jordan-ci-trigger
  Scenario: Jordan triggers a pipeline from CI on GitHub releases
    Given my OSS repo has GitHub Releases
    When a new release is published
    And the webhook trigger fires
    Then my "changelog-generator" pipeline starts
    And the pipeline posts release notes to my issue tracker

  @goal-jordan-no-enterprise-friction
  Scenario: Jordan uses the community library without enterprise setup
    Given I am a solo developer using Community edition
    When I browse, copy, and contribute library primitives
    Then I can do all of this without SSO, team setup, or a licence key
    And I never need to enter payment information
