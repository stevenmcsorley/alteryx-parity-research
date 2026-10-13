import argparse
from pathlib import Path
from .runtime import run_from_file, WorkflowError

def main():
    p=argparse.ArgumentParser(description='Run an original, limited, five-operator data-flow testbed (NOT Alteryx-compatible).')
    p.add_argument('workflow',type=Path)
    args=p.parse_args()
    try:
        outputs=run_from_file(args.workflow.resolve())
        for name,table in outputs.items(): print(f'{name}: {len(table.rows)} rows, {len(table.columns)} fields')
    except (WorkflowError, OSError) as e:p.exit(1,f'Workflow error: {e}\n')

if __name__=='__main__':main()
