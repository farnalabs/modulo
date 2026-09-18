Feature: SSRF-safe Outbound URL Validation
  As a security administrator
  I want every outbound network hop to be validated against internal / metadata
    targets before a connection is made
  So that tenant-supplied URLs can never pivot onto loopback, cloud metadata or
    other private ranges (feat-core-ssrf)

  Scenario Outline: Private / metadata / CGNAT literal targets fail closed
    Given the global egress allowlist is empty
    When I validate the outbound URL "<url>"
    Then the URL is rejected with "private/internal network address"

    Examples:
      | url                                        |
      | http://127.0.0.1:8080/admin                |
      | http://10.0.0.5/                           |
      | http://192.168.1.1/                        |
      | http://169.254.169.254/latest/meta-data/   |
      | http://100.64.0.1/                         |
      | http://100.100.100.200/latest/meta-data/   |
      | http://0.0.0.0/                            |

  Scenario Outline: URL syntax violations are rejected before any DNS
    When I validate the outbound URL "<url>"
    Then the URL is rejected with "<reason>"

    Examples:
      | url                          | reason                                          |
      | ftp://example.com/file       | must use http:// or https:// scheme             |
      | http://user:pass@example.com | userinfo credentials                            |
      | http:///path                 | valid hostname                                  |
      | http://172.16.0.1:0/         | port is out of the valid range                  |
      | http://2130706433/           | decimal/octal integer IP literal                |
      | http://0x7f000001/           | hex-encoded IP literal                          |
      | http://127.1/                | non-canonical dotted-numeric IP literal         |

  Scenario Outline: A hostname that resolves to any blocked address is refused
    Given the hostname "<host>" resolves to "<ips>"
    When I validate the outbound URL "http://<host>/"
    Then the URL is rejected with "resolves to a private/internal address"

    Examples:
      | host                | ips                     |
      | collector.internal  | 10.1.2.3                |
      | mixed.example.com   | 93.184.216.34,10.0.0.5  |

  Scenario: A hostname that resolves to a public address is accepted
    Given the hostname "api.example.com" resolves to "93.184.216.34"
    When I validate the outbound URL "https://api.example.com/"
    Then the URL is accepted

  Scenario: An empty resolution fails closed
    Given the hostname "ghost.example.com" resolves to no addresses
    When I validate the outbound URL "http://ghost.example.com/"
    Then the URL is rejected with "resolved to no addresses"

  Scenario: Public literal IPs are accepted without DNS
    Given the global egress allowlist is empty
    When I validate the outbound URL "http://8.8.8.8/"
    Then the URL is accepted

  Scenario: The global egress allowlist may permit private ranges
    Given the global egress allowlist is "10.0.0.0/8,192.168.0.0/16"
    When I validate the outbound URL "http://10.1.2.3/"
    Then the URL is accepted

  Scenario: The allowlist can never weaken the non-negotiable blocked floor
    Given the global egress allowlist is "169.254.0.0/16,100.64.0.0/10"
    When I validate the outbound URL "http://169.254.169.254/"
    Then the URL is rejected with "private/internal network address"

  Scenario: The tenant-scoped allowlist layers on the global floor
    Given the global egress allowlist is empty
    When I validate the outbound URL "http://10.1.2.3/" allowlisting "10.0.0.0/8"
    Then the URL is accepted

  Scenario: Async validation reports the same fail-closed verdict
    Given the hostname "collector.internal" resolves asynchronously to "192.168.0.10"
    When I validate the outbound URL "http://collector.internal:4318/" asynchronously
    Then the URL is rejected with "resolves to a private/internal address"

  Scenario: Async validation accepts a public hostname
    Given the hostname "api.example.com" resolves asynchronously to "93.184.216.34"
    When I validate the outbound URL "https://api.example.com/" asynchronously
    Then the URL is accepted

  Scenario: The pinned target keeps the original hostname with the validated address set
    Given the hostname "api.example.com" resolves asynchronously to "93.184.216.34,1.1.1.1"
    When I resolve the pinned target for "https://api.example.com/"
    Then the pinned target keeps hostname "api.example.com" and addresses "93.184.216.34,1.1.1.1"

  Scenario: The pinned transport refuses any unpinned host
    Given the hostname "example.com" resolves to "93.184.216.34"
    When I build a pinned async transport for "https://example.com/"
    Then the pinned transport refuses the unpinned host "evil.example"

  Scenario: The pinned client factories reject a caller-supplied transport
    When I build a pinned async client for "https://example.com/" with a caller-supplied transport
    Then the URL build fails with "must not be passed via client_kwargs"
