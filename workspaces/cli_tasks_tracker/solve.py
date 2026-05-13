'''
Solve remaining test failures by running full pytest and analyzing output.
Then fix actual issues found.
'''
import subprocess
import sys

result = subprocess.run([sys.executable, '-m', 'pytest', 'tests', '-v'], capture_output=True, text=True)
print(result.stdout)
print(result.stderr)
print(f'Exit code: {result.returncode}')