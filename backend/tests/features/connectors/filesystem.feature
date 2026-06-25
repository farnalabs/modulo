Feature: Filesystem Connector
  As a pipeline author
  I want to read and write files on the local filesystem
  So that my agents can process file-based data

  Background:
    Given I am authenticated in org "acme"

  Scenario: Connector reads a file
    Given a filesystem connector configured with base_path "/data"
    When the connector reads "input.txt"
    Then the connector returns the file content

  Scenario: Connector writes a file
    Given a filesystem connector configured with base_path "/data"
    When the connector writes "output.txt" with content "hello"
    Then the file "output.txt" exists with content "hello"

  Scenario: Path traversal outside base_path is blocked
    Given a filesystem connector configured with base_path "/data"
    When the connector tries to read "../../etc/passwd"
    Then the operation is rejected with a security error

  Scenario: Connector lists directory contents
    Given a filesystem connector configured with base_path "/data"
    When the connector lists the directory "."
    Then the result includes the files in the directory

  Scenario: Non-existent file returns error
    Given a filesystem connector configured with base_path "/data"
    When the connector reads "nonexistent.txt"
    Then the operation returns a "not_found" error
