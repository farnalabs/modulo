Feature: Runtime provider file-I/O primitives (FAR-1050 R2a)
  As a runtime provider consumer
  I want the provider-neutral read/write/list/stat primitives on the RuntimeProvider ABC
  So that workspace files exchange binary-safe across every tier without shell-specific call sites

  Background:
    Given a local runtime workspace

  Scenario: A file written through the exec-based default reads back byte-exact
    When I write "hello world" to file "notes.txt"
    Then reading file "notes.txt" returns "hello world"

  Scenario: Parent directories are created on write
    When I write "nested payload" to file "data/a/b/deep.txt"
    Then reading file "data/a/b/deep.txt" returns "nested payload"

  Scenario: Binary bytes survive the base64 text exec channel
    When I write 256 binary bytes to file "bin.dat"
    Then reading file "bin.dat" returns 256 binary bytes

  Scenario: Overwriting a file replaces its contents
    When I write "first version" to file "overwrite.txt"
    And I write "second version" to file "overwrite.txt"
    Then reading file "overwrite.txt" returns "second version"

  Scenario: Reading a missing file raises the typed runtime error
    When I read missing file "missing.txt"
    Then the file operation fails with the typed runtime provider error

  Scenario: Listing a directory returns the sorted child paths including hidden entries
    Given a file "a.txt" containing "visible"
    And a file ".hidden" containing "hidden"
    And a directory "sub" with a file inside
    When I list files in directory "."
    Then the listing is exactly "./.hidden", "./a.txt", "./sub"

  Scenario: Listing a missing directory fails with the typed runtime error
    When I list files in directory "no-such-dir"
    Then the file operation fails with the typed runtime provider error

  Scenario: get_info reports the size and directory flag for a file and a directory
    Given a file "data.txt" that is 5 bytes long
    And a directory "folder" with a file inside
    When I get info for "data.txt"
    Then the info shows a size of 5 bytes and is not a directory
    When I get info for "folder"
    Then the info path is a directory

  Scenario: get_info on a missing path fails with the typed runtime error
    When I get info for "ghost.txt"
    Then the file operation fails with the typed runtime provider error