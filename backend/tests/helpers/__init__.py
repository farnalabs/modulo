"""Test helpers for isolated org testing.

Usage:
    from tests.helpers.isolated_org import IsolatedOrgContext, create_isolated_org, destroy_isolated_org

    async with create_isolated_org(base_url, admin_token) as ctx:
        # ctx.org_id, ctx.user_email, ctx.user_password
        # Tests run in an org that no other test touches
"""