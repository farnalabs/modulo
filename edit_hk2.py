import sys
p = sys.argv[1]
with open(p) as f:
    c = f.read()
old = '      - name: List all open PRs'
new = '      - name: Check main CI\n        id: check-main\n        env:\n          GH_TOKEN: ${{ secrets.MODULO_REVIEWBOT_TOKEN || github.token }}\n          BF_WEBHOOK_URL: ${{ secrets.MODULO_BRANCH_FIXER_WEBHOOK_URL }}\n          BF_HMAC_SECRET: ${{ secrets.MODULO_BRANCH_FIXER_HMAC_SECRET }}\n        shell: bash\n        run: |\n          CI_STATUS=$(gh run list --repo farnalabs/modulo --workflow CI --branch main --limit 1 --json conclusion --jq '"'"'.[0].conclusion'"'"' 2>/dev/null || echo '"'"'unknown'"'"')\n          echo '"'"'Main CI: '"'"'$CI_STATUS\n          if [ '"'"'"$CI_STATUS"'"'"' = '"'"'failure'"'"' ]; then\n            echo '"'"'main_ci=failing'"'"' >> $GITHUB_OUTPUT\n            if [ -n '"'"'"$BF_WEBHOOK_URL"'"'"' ]; then\n              TS=$(date +%s)\n              BODY=$(jq -n --arg bn '"'"'main'"'"' --arg fd '"'"'Main CI failing'"'"' '"'"'{branchName: $bn, failureDescription: $fd}'"'"')\n              HMAC=$(echo -n '"'"'"$TS.$BODY"'"'"' | openssl dgst -sha256 -hmac '"'"'"$BF_HMAC_SECRET"'"'"' | sed '"'"'s/^.* //'"'"')\n              curl -s -X POST '"'"'"$BF_WEBHOOK_URL"'"'"' -H '"'"'Content-Type: application/json'"'"' -H '"'"'X-Modulo-Timestamp: $TS'"'"' -H '"'"'X-Modulo-Webhook-Secret: sha256=$HMAC'"'"' -d '"'"'"$BODY"'"'"' || true\n            fi\n          else\n            echo '"'"'main_ci=ok'"'"' >> $GITHUB_OUTPUT\n          fi\n\n      - name: List all open PRs'
c = c.replace(old, new, 1)
with open(p, 'w') as f:
    f.write(c)
print('Done')
