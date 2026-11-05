// SPDX-License-Identifier: AGPL-3.0-or-later
function toolLabel(name, args = {}) {
  const target = String(args.path || args.query || args.pattern || '').slice(0, 160);
  const labels = {read_file: 'Reading', list_dir: 'Browsing', search_files: 'Searching',
    write_file: 'Writing', edit_file: 'Editing', append_file: 'Updating', validate_file: 'Checking',
    todo_write: 'Updating the plan', run_shell: 'Running', git_status: 'Checking Git',
    delegate: 'Working on a subtask', browser: 'Using the browser', computer: 'Using the computer'};
  return (labels[name] || name.replaceAll('_', ' ')) + (target ? ` ${target}` : name === 'run_shell' ? ` ${String(args.command || 'a command').slice(0, 160)}` : '');
}
module.exports = {toolLabel};
