from e2b import Template
import os

t = Template(file_context_path=os.path.dirname(os.path.abspath(__file__)))
b = t.from_template("base")

b.apt_install(["jq"])
b.pip_install(["requests", "pyyaml"])
b.copy("modulo-wrap.sh", "/home/user/modulo-wrap.sh")
b.run_cmd("cp /home/user/modulo-wrap.sh /home/user/review.py")  # copy and we'll fix content later
b.run_cmd("chmod +x /home/user/modulo-wrap.sh")

result = t.build(b, name="modulo-agent", tags=["modulo", "agent"])
print(f"Built: {result.template_id}")
