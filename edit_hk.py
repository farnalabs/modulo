import sys
p = sys.argv[1]
with open(p) as f:
    c = f.read()
old = '      - name: List all open PRs'
c = c.replace(old, '''
c = c.replace("if: steps.list.outputs.count > 0", "if: steps.check-main.outputs.main_ci == 'ok' && steps.list.outputs.count > 0", 1)
with open(p, 'w') as f:
    f.write(c)
print('Done')