# SPDX-License-Identifier: AGPL-3.0-or-later
import tempfile
import os
import shlex
import subprocess
import sys
from unittest.mock import patch
import unittest
from pathlib import Path

import evidence


class CheckClassification(unittest.TestCase):
    def test_reads_and_file_writes_are_not_checks(self):
        for command in ("ls -la", "cat game.cpp | head", "cd src && grep -n score *.cpp",
                        "cat > t.cpp << 'EOF'\nint main() { return 0; }\nEOF", "pkg-config --cflags sdl2"):
            self.assertFalse(evidence.is_check(command), command)

    def test_builds_and_test_runs_are_checks(self):
        for command in ("g++ -o t t.cpp && ./t", "node test.js", "python3 -m unittest",
                        "bash -c 'cat > t.cpp << \"EOF\"\nint main(){}\nEOF\ng++ t.cpp -o t'"):
            self.assertTrue(evidence.is_check(command), command)

    def test_commit_messages_are_not_read_as_commands(self):
        body = "Add flow test\n\n- test/flow.js: end-to-end flow test\n- src/a.js: helper"
        for command in (f'cd X && git add test/flow.js && git commit -m "{body}"',
                        f"cd X && git commit -m '{body}'",
                        "cd X && git commit -m 'one; two && three | four'",
                        'git commit -m "say \\"hi\\"\n- node test.js"',
                        "git commit -m \"$(cat <<'EOF'\nsubject\n\n- body line\nEOF\n)\"",
                        'git commit \\\n  -m "subject"',
                        "bash -c 'cd X && git commit -m \"subject\n- body line\"'"):
            self.assertFalse(evidence.is_check(command), command)

    def test_real_checks_still_count_beside_quotes(self):
        for command in ("node test/flow.js", "npm test", "cd X && node test/flow.js", "cd X && npm test",
                        'cd X && git commit -m "subject\n- body" && npm test',
                        "npm test # don't skip\ngit status",
                        "# don't skip\nnpm test\n# can't skip",
                        "echo it's && npm test",
                        "bash -c 'cd X && npm test'"):
            self.assertTrue(evidence.is_check(command), command)

    def test_heredoc_bodies_do_not_count_as_write_targets(self):
        targets = evidence.written_paths("cat > t.cpp << 'EOF'\nif (a > limit) return;\nEOF\n")
        self.assertEqual(targets, {"t.cpp"})

    def test_quoted_redirects_are_not_write_targets(self):
        for command in ('git commit -m "rename a -> b.js"',
                        "git commit -m 'rename a -> b.js'",
                        'git commit -m "subject\n\n- moved a -> b.js\n- piped | tee c.log\n- see d > e.txt"',
                        "git commit -m 'subject\n\n- moved a -> b.js\n- piped | tee c.log'",
                        'git commit -m "don\'t move a -> b.js"',
                        "git commit -m \"$(cat <<'EOF'\nsubject\n\nmoved a -> b.js\nEOF\n)\"",
                        'echo "tee foo.txt"', 'grep -n "a > b" x.py', 'python3 -c "print(1 > 0)"',
                        "echo a\\>b", "ls # see a > b.txt", "npm test > /dev/null 2>&1"):
            self.assertEqual(evidence.written_paths(command), set(), command)

    def test_real_writes_still_count_beside_quotes(self):
        for command, targets in (("echo x > out.txt", {"out.txt"}), ("echo x >> out.txt", {"out.txt"}),
                                 ('echo x > "out.txt"', {"out.txt"}), ("echo x >'out.txt'", {"out.txt"}),
                                 ("npm test 2> err.log", {"err.log"}), ("echo it's > k.txt", {"k.txt"}),
                                 ("echo x | tee log.txt", {"log.txt"}), ("echo x | tee -a log.txt", {"log.txt"}),
                                 ('echo x | tee "log.txt"', {"log.txt"}),
                                 ("sed -i 's/a/b/' cart.js", {"cart.js"}), ('sed -i "s/a/b/" cart.js', {"cart.js"}),
                                 ("sed -i 's/a > b/c/' cart.js", {"cart.js"}), ("sed -i 's|a|b|' cart.js", {"cart.js"}),
                                 ("sed -i.bak -e 's/a/b/' cart.js && npm test", {"cart.js"}),
                                 ("perl -pi -e 's/a/b/' cart.js", {"cart.js"}),
                                 ('cat > a.txt && git commit -m "x -> y.js"', {"a.txt"})):
            self.assertEqual(evidence.written_paths(command), targets, command)

    def test_writes_inside_a_quoted_script_still_count(self):
        for command, targets in (("bash -c 'cat > t.cpp << \"EOF\"\nint main(){}\nEOF\ng++ t.cpp -o t'", {"t.cpp"}),
                                 ("bash -c 'echo x > f.txt'", {"f.txt"}),
                                 ('sh -c "echo \\"y > z\\" > g.txt"', {"g.txt"}),
                                 ("sudo bash -c 'echo x > /etc/h.conf'", {"/etc/h.conf"}),
                                 ("cd X && bash -lc 'npm test | tee i.log'", {"X/i.log"}),
                                 ('OUT="$(npm test | tee j.log)"', {"j.log"}),
                                 ("bash -c 'git commit -m \"a -> b.js\"'", set())):
            self.assertEqual(evidence.written_paths(command), targets, command)

    def test_quoted_and_escaped_targets_keep_their_whole_name(self):
        for command, targets in (('echo x > "my file.txt"', {"my file.txt"}),
                                 ("echo x >> 'my file.txt'", {"my file.txt"}),
                                 ("echo x > my\\ file.txt", {"my file.txt"}),
                                 ('echo x > "my dir"/file.txt', {"my dir/file.txt"}),
                                 ('echo x | tee "my file.txt"', {"my file.txt"}),
                                 ("echo x | tee -a my\\ file.txt", {"my file.txt"}),
                                 ("echo x | tee a.log b.log", {"a.log", "b.log"}),
                                 ("OUT=$(npm test | tee j.log)", {"j.log"}),
                                 ("echo x > C:\\tmp\\a.txt", {"C:\\tmp\\a.txt"})):
            self.assertEqual(evidence.written_paths(command), targets, command)

    def test_in_place_edits_read_quoted_files(self):
        for command, targets in (("sed -i 's/a/b/' \"file.txt\"", {"file.txt"}),
                                 ("sed -i 's/a/b/' 'my file.txt'", {"my file.txt"}),
                                 ("sed -i 's/a/b/' my\\ file.txt", {"my file.txt"}),
                                 ("sed -i 's/a/b/' a.txt \"b.txt\"", {"a.txt", "b.txt"}),
                                 ("sed -i -e 's/a/b/' -e 's/c/d/' \"file.txt\"", {"file.txt"}),
                                 ("sed --in-place=.bak 's/a/b/' \"file.txt\"", {"file.txt"}),
                                 ("sed -i '' 's/a/b/' \"file.txt\"", {"file.txt"}),
                                 ("sudo sed -i 's/a/b/' \"file.txt\" && npm test", {"file.txt"}),
                                 ("sed -i 's/a/b/' cart.js 2>/dev/null", {"cart.js"}),
                                 ("perl -i -pe 's/a/b/' \"file.txt\"", {"file.txt"}),
                                 ("perl -pi -e 's/a/b/' \"file.txt\"", {"file.txt"}),
                                 ("perl -i.bak -pe 's/a/b/' \"file.txt\"", {"file.txt"}),
                                 ("perl -lpi -e 's/a/b/' cart.js", {"cart.js"}),
                                 ("bash -c 'sed -i \"s/a/b/\" \"file.txt\"'", {"file.txt"})):
            self.assertEqual(evidence.written_paths(command), targets, command)

    def test_reads_by_sed_and_perl_are_not_writes(self):
        for command in ("sed 's/a/b/' \"file.txt\"", "sed -n '1,2p' file.txt", "sed -i 's/a/b/'",
                        "perl -pe 's/i/b/' \"file.txt\"", "perl -Mitems -e 'print 1' file.txt",
                        'grep "tee" foo.py', 'grep "sed" -i foo.py',
                        "find . -name '*.js' -exec sed -i 's/a/b/' {} +",
                        "find . -name '*.js' -exec sed -i 's/a/b/' {} \\;"):
            self.assertEqual(evidence.written_paths(command), set(), command)

    def test_a_heredoc_keeps_the_rest_of_its_opening_line(self):
        for command, targets in (("cat <<EOF > out.txt\nbody > no.txt\nEOF\n", {"out.txt"}),
                                 ("cat <<'EOF' >> out.txt\nbody\nEOF", {"out.txt"}),
                                 ("cat <<EOF | tee log.txt\nbody\nEOF\n", {"log.txt"}),
                                 ("cat <<A > a.txt\nx\nA\ncat <<B > b.txt\ny\nB\n", {"a.txt", "b.txt"})):
            self.assertEqual(evidence.written_paths(command), targets, command)
        self.assertTrue(evidence.is_check("cat <<EOF | node x.js\nbody\nEOF\n"))
        self.assertFalse(evidence.is_check("cat <<EOF > out.txt\nnode x.js\nEOF\n"))

    def test_cp_and_mv_write_their_destination(self):
        for command, targets in (("cp a.txt b.txt", {"b.txt"}), ("cp -f a.txt b.txt", {"b.txt"}),
                                 ("cp a.txt dir/", {"dir/a.txt"}), ("cp x/a.txt .", {"a.txt"}),
                                 ("cp a.txt b.txt dir", {"dir/a.txt", "dir/b.txt"}),
                                 ("cp -r src/ dest/", {"dest/src"}),
                                 ("cp -t dir a.txt b.txt", {"dir/a.txt", "dir/b.txt"}),
                                 ("cp --target-directory=dir a.txt", {"dir/a.txt"}),
                                 ("cp --target-directory dir a.txt", {"dir/a.txt"}),
                                 ("cp -T a.txt dir/", {"dir/"}), ("cp -S .old a.txt b.txt", {"b.txt"}),
                                 ("cp a.txt 'my file.txt'", {"my file.txt"}), ("sudo cp a.txt b.txt", {"b.txt"}),
                                 ("cp a.txt", set())):
            self.assertEqual(evidence.written_paths(command), targets, command)

    def test_mv_also_takes_away_its_source_and_rm_its_operands(self):
        for command, targets in (("mv a b", {"a", "b"}), ("mv a.txt dir/", {"a.txt", "dir/a.txt"}),
                                 ("mv -t dir a.txt", {"a.txt", "dir/a.txt"}),
                                 ("rm a.txt", {"a.txt"}), ("rm -rf a.txt b/", {"a.txt", "b/"}),
                                 ("\\rm a.txt", {"a.txt"}), ("sudo rm a.txt", {"a.txt"})):
            self.assertEqual(evidence.written_paths(command), targets, command)

    def test_a_destination_on_disk_decides_whether_it_is_a_directory(self):
        is_dir = {"dir"}.__contains__
        self.assertEqual(evidence.written_paths("cp a.txt dir", is_dir), {"dir/a.txt"})
        self.assertEqual(evidence.written_paths("cp a.txt other", is_dir), {"other"})
        self.assertEqual(evidence.written_paths("cp -T a.txt dir", is_dir), {"dir"})
        self.assertEqual(evidence.written_paths("bash -c 'cp a.txt dir'", is_dir), {"dir/a.txt"})

    def test_comparisons_are_not_redirects(self):
        for command in ("echo $((a > b))", "n=$((a > b))", "(( a > b ))", "(( (a + 1) > b )) && ls",
                        "[[ a > b ]]", "[[ -f x && a > b ]] && ls", '[[ "a ]]" > b ]]',
                        "if [[ a > b ]]; then ls; fi", "for ((i = 0; i > 3; i++)); do ls; done"):
            self.assertEqual(evidence.written_paths(command), set(), command)

    def test_real_redirects_still_count_beside_comparisons(self):
        for command, targets in (("echo $((a > b)) > c", {"c"}), ("[[ a > b ]] && echo x > c", {"c"}),
                                 ("(( a > b )) && echo x > c", {"c"}), ("[ a > b ]", {"b"}),
                                 ("((echo x > c) ; ls)", {"c"}), ("echo $(($(ls > c) + 1))", {"c"}),
                                 ('[[ "$(ls > c)" > b ]]', {"c"}), ("[[ a > b", {"b"})):
            self.assertEqual(evidence.written_paths(command), targets, command)

    def test_an_in_place_perl_is_an_edit_not_a_check(self):
        for command in ("perl -pi -e 's/a/b/' cart.js", "ls | xargs perl -pi -e 's/a/b/'"):
            self.assertFalse(evidence.is_check(command), command)
        self.assertTrue(evidence.is_check("perl run.pl"))
        self.assertTrue(evidence.is_check("perl -pe 's/a/b/' cart.js"))

    def test_a_tool_name_as_an_argument_is_not_that_tool(self):
        for command in ("grep tee foo.py", "grep sed -i foo.py", "grep perl -i foo.py", "echo tee foo.py",
                        "git log --grep tee foo.py", "./run tee foo.py", "cd sed -i foo.py",
                        "grep rm foo.py", "echo cp a b", "git mv -k mv a b", "ls mv a b",
                        "find . -name tee foo.py", "find . -exec grep tee foo.py {} +",
                        "xargs grep tee foo.py", "sudo grep tee foo.py", "cat x | grep tee foo.py",
                        "sudo -u root grep tee foo.py", "timeout 5 grep tee foo.py"):
            self.assertEqual(evidence.written_paths(command), set(), command)

    def test_a_tool_behind_a_wrapper_or_keyword_still_counts(self):
        sed = "sed -i 's/a/b/' f.js"
        for command, targets in (("sudo tee f.log", {"f.log"}), ("sudo -u root tee f.log", {"f.log"}),
                                 ("sudo -Eu root tee f.log", {"f.log"}), ("sudo --user=root tee f.log", {"f.log"}),
                                 ("sudo --user root tee f.log", {"f.log"}), ("sudo -E tee f.log", {"f.log"}),
                                 ("sudo --preserve-env tee f.log", {"f.log"}), ("sudo -- tee f.log", {"f.log"}),
                                 ("env A=1 B=2 tee f.log", {"f.log"}), ("A=1 tee f.log", {"f.log"}),
                                 ("timeout 5 tee f.log", {"f.log"}), ("timeout -k 3 5 tee f.log", {"f.log"}),
                                 ("nice -n 5 tee f.log", {"f.log"}), ("stdbuf -oL tee f.log", {"f.log"}),
                                 ("time tee f.log", {"f.log"}), ("nohup tee f.log", {"f.log"}),
                                 ("sudo env A=1 nice tee f.log", {"f.log"}), ('"tee" f.log', {"f.log"}),
                                 ("/usr/bin/tee f.log", {"f.log"}), ("(tee f.log)", {"f.log"}),
                                 ("! tee f.log", {"f.log"}), ("{ tee f.log; }", {"f.log"}),
                                 ("echo `tee f.log`", {"f.log"}),
                                 (f"xargs {sed}", {"f.js"}), (f"xargs -0 -n 1 {sed}", {"f.js"}),
                                 (f"xargs -I{{}} {sed}", {"f.js"}), (f"xargs -I {{}} {sed}", {"f.js"}),
                                 (f"sudo xargs {sed}", {"f.js"}), (f"sudo {sed}", {"f.js"}),
                                 (f"for x in a; do {sed}; done", {"f.js"}), (f"if ls; then {sed}; fi", {"f.js"}),
                                 (f"if ls; then ls; else {sed}; fi", {"f.js"}), (f"while ls; do {sed}; done", {"f.js"}),
                                 (f"case x in a) {sed} ;; esac", {"f.js"}),
                                 (f"find . -exec {sed} \\;", {"f.js"}), (f"find . -execdir {sed} \\;", {"f.js"}),
                                 (f"find . -exec sudo {sed} \\;", {"f.js"}), ("find . -exec tee f.log \\;", {"f.log"}),
                                 (f"find . -exec grep a {{}} + -exec {sed} \\;", {"f.js"})):
            self.assertEqual(evidence.written_paths(command), targets, command)

    def test_files_a_command_reaches_by_pattern_are_not_named(self):
        for command in ("find . -exec sed -i 's/a/b/' {} +", "xargs sed -i 's/a/b/'", "xargs rm",
                        "sed -i 's/a/b/' *.js", 'sed -i \'s/a/b/\' "$f"', "echo x > $OUT", "rm src/{a,b}.js",
                        "cp *.js dir/", "find . -exec cp {} dir/ \\;", "find . -delete"):
            self.assertEqual(evidence.written_paths(command), set(), command)

    def test_cd_moves_the_paths_after_it(self):
        for command, targets in (("cd src && sed -i 's/a/b/' f.js", {"src/f.js"}), ("cd src; echo x > f.js", {"src/f.js"}),
                                 ("echo x > f.js && cd src", {"f.js"}), ("cd src > log.txt", {"log.txt"}),
                                 ("cd src && cd .. && rm f.js", {"f.js"}), ("cd a/b && rm ../f.js", {"a/f.js"}),
                                 ("cd -P src && rm f.js", {"src/f.js"}), ("cd -- src && rm f.js", {"src/f.js"}),
                                 ("cd /srv/app && rm f.js", {"/srv/app/f.js"}), ("cd src && rm /srv/f.js", {"/srv/f.js"}),
                                 ("if cd src; then rm f.js; fi", {"src/f.js"}), ("pushd src && rm f.js", {"src/f.js"}),
                                 ("cd src && bash -c 'rm f.js'", {"src/f.js"}), ("bash -c 'cd src && rm f.js'", {"src/f.js"}),
                                 ('cd src && OUT="$(tee f.log)"', {"src/f.log"}), ("cd src && cp a.js b.js", {"src/b.js"}),
                                 ("git -C src rm f.js", {"src/f.js"}), ("cd a && git -C b rm f.js", {"a/b/f.js"})):
            self.assertEqual(evidence.written_paths(command), targets, command)
        self.assertEqual(evidence.written_paths("cd src && cp a.txt dir", {"src/dir"}.__contains__), {"src/dir/a.txt"})

    def test_a_cd_in_a_subshell_ends_with_it(self):
        for command, targets in (("(cd src && rm a.js); rm b.js", {"src/a.js", "b.js"}), ("OUT=$(cd src && ls); rm b.js", {"b.js"}),
                                 ("echo `cd src && ls`; rm b.js", {"b.js"}), ("bash -c 'cd src'; rm b.js", {"b.js"}),
                                 ('OUT="$(cd src)"; rm b.js', {"b.js"}), ("{ cd src; }; rm b.js", {"src/b.js"})):
            self.assertEqual(evidence.written_paths(command), targets, command)

    def test_after_a_cd_to_an_unread_place_only_absolute_paths_are_named(self):
        for command in ('cd "$d" && rm f.js', "cd && rm f.js", "cd - && rm f.js", "cd ~/x && rm f.js",
                        "pushd a && popd && rm f.js", 'cd "$d" && cd sub && rm f.js'):
            self.assertEqual(evidence.written_paths(command), set(), command)
            self.assertIn(("unknown", ""), list(evidence.file_effects(command)), command)
        self.assertEqual(evidence.written_paths('cd "$d" && rm /srv/f.js'), {"/srv/f.js"})
        self.assertEqual(evidence.written_paths('cd "$d" && cd /srv && rm f.js'), {"/srv/f.js"})

    def test_other_tools_that_write_files(self):
        for command, targets in (("touch a.js b.js", {"a.js", "b.js"}), ("touch -d 2020-01-01 a.js", {"a.js"}),
                                 ("touch -r ref.js a.js", {"a.js"}), ("touch --date 2020-01-01 a.js", {"a.js"}),
                                 ("touch -c a.js", set()), ("touch --no-create a.js", set()),
                                 ("ln -s a.js b.js", {"b.js"}), ("ln -sf a.js b.js", {"b.js"}), ("ln a.js dir/", {"dir/a.js"}),
                                 ("ln -t dir a.js", {"dir/a.js"}), ("ln -s ../x/a.js", {"a.js"}),
                                 ("install a.js b.js", {"b.js"}), ("install -m 755 a.js b.js", {"b.js"}),
                                 ("install -Dm755 a.js b.js", {"b.js"}), ("install --mode 755 a.js b.js", {"b.js"}),
                                 ("install -o me -g us a.js dir/", {"dir/a.js"}), ("install -t dir a.js", {"dir/a.js"}),
                                 ("install -d new", set()), ("install -d new other", set()), ("install -dm755 new other", set()),
                                 ("install --directory new other", set()),
                                 ("install -odan a.js b.js", {"b.js"}),
                                 ("dd if=a.js of=b.js", {"b.js"}), ("dd of=b.js bs=1", {"b.js"}), ("dd if=a.js", set()),
                                 ("dd if=a.js of=/dev/null", set()), ("sudo install a.js b.js", {"b.js"})):
            self.assertEqual(evidence.written_paths(command), targets, command)
        for command in ("install a.js b.js", "dd if=a.js of=b.js", "pushd src", "popd", "touch a.js", "ln -s a b"):
            self.assertFalse(evidence.is_check(command), command)
        for command in ("npm install", "make install", "pip install x"):
            self.assertTrue(evidence.is_check(command), command)

    def test_git_commands_that_remove_files_name_them(self):
        for command, targets in (("git rm a.js", {"a.js"}), ("git rm -rf a.js b/", {"a.js", "b/"}), ("git rm -- a.js", {"a.js"}),
                                 ("git -c x=y rm a.js", {"a.js"}), ("git --no-pager rm a.js", {"a.js"}), ("sudo git rm a.js", {"a.js"}),
                                 ("git rm --cached a.js", set()), ("git rm -r --cached a.js", set()),
                                 ("git rm -n a.js", set()), ("git rm --dry-run a.js", set()), ("git rm 'src/*.js'", set()),
                                 ("git mv rm a.js", set()), ("git commit -m rm a.js", set()), ("git log rm a.js", set())):
            self.assertEqual(evidence.written_paths(command), targets, command)

    def test_checks_behind_a_wrapper_are_read_by_what_they_run(self):
        for command in ("sudo npm test", "xargs node x.js", "timeout 5 ./run", "for x in a; do npm test; done"):
            self.assertTrue(evidence.is_check(command), command)
        for command in ("ls | xargs sed -i 's/a/b/'", "sudo sed -i 's/a/b/' x", "timeout 5 cat x",
                        "for x in a; do sed -i 's/a/b/' \"$x\"; done",
                        "if [[ -f x ]]; then cat x; fi"):
            self.assertFalse(evidence.is_check(command), command)


class LedgerRecord(unittest.TestCase):
    """Parser fallback tests simulate commands without executing them."""
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(); self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        (self.root / "test.js").write_text("// existing\n")
        (self.root / "cart.js").write_text("// source\n")
        self.ledger = evidence.Ledger(self.root, observe_shell=False)

    def edit(self):
        self.ledger.after_write("edit_file", "cart.js", {"ok": True, "changed": True})

    def test_a_commit_is_not_recorded_as_a_check(self):
        self.edit()
        self.ledger.after_shell('cd X && git add test.js && git commit -m "Add test\n\n- test.js: flow test"', 0)
        self.assertEqual(self.ledger.status(), ("none", []))

    def test_a_test_written_this_run_is_labelled_as_its_own(self):
        self.edit()
        command = "cat > test_cart.cpp << 'EOF'\nint main(){}\nEOF\ng++ -o test_cart test_cart.cpp && ./test_cart"
        self.ledger.before_shell(command)
        self.ledger.after_shell(command, 0)
        self.assertEqual(self.ledger.status()[0], "own")

    def test_an_existing_test_is_labelled_existing(self):
        self.edit()
        self.ledger.before_shell("node test.js")
        self.ledger.after_shell("node test.js", 0)
        self.assertEqual(self.ledger.status()[0], "existing")

    def test_building_or_running_the_app_is_not_a_test(self):
        self.edit()
        self.ledger.after_shell("g++ -o cart cart.cpp && ./cart", 0)
        self.assertEqual(self.ledger.status()[0], "ran")

    def test_a_learned_build_command_is_not_a_test(self):
        self.edit()
        report = {"commands": [{"command": "g++ cart.cpp", "kind": "build", "result": {"exit_code": 0}}]}
        self.assertEqual(self.ledger.status(report)[0], "ran")

    def test_a_discovered_test_command_is_a_test(self):
        self.edit()
        report = {"commands": [{"command": "npm test", "kind": "test", "result": {"exit_code": 0}}]}
        self.assertEqual(self.ledger.status(report)[0], "existing")

    def test_a_test_runner_counts_as_existing_until_a_test_was_touched(self):
        self.edit()
        self.ledger.after_shell("python3 -m unittest", 0)
        self.assertEqual(self.ledger.status()[0], "existing")
        self.ledger.after_write("edit_file", "test_cart.py", {"ok": True, "changed": True})
        self.ledger.after_shell("python3 -m unittest", 0)
        self.assertEqual(self.ledger.status()[0], "own")

    def test_an_existing_test_the_run_edited_is_its_own(self):
        self.ledger.after_write("edit_file", "test.js", {"ok": True, "changed": True})
        self.ledger.after_shell("node test.js", 0)
        self.assertEqual(self.ledger.status()[0], "own")

    def test_a_retry_with_other_flags_replaces_the_failed_attempt(self):
        self.edit()
        self.ledger.after_shell("g++ -lbad cart.cpp -o cart", 1)
        self.assertEqual(self.ledger.status()[0], "failing")
        self.ledger.after_shell("g++ -lgood cart.cpp -o cart", 0)
        self.assertEqual(self.ledger.status()[0], "ran")

    def test_checks_before_the_last_edit_do_not_count(self):
        self.ledger.after_shell("node test.js", 0)
        self.edit()
        self.assertEqual(self.ledger.status()[0], "none")

    def test_the_latest_run_of_a_check_decides(self):
        self.edit()
        self.ledger.after_shell("node test.js", 1)
        self.assertEqual(self.ledger.status()[0], "failing")
        self.ledger.after_shell("node test.js", 0)
        self.assertEqual(self.ledger.status()[0], "existing")

    def test_rewriting_an_existing_file_through_the_shell_is_an_edit(self):
        self.ledger.after_shell("node test.js", 0)
        self.ledger.before_shell("sed -i 's/a/b/' cart.js")
        self.assertEqual(self.ledger.status()[0], "none")

    def test_a_commit_message_naming_a_file_is_not_an_edit(self):
        self.ledger.after_shell("node test.js", 0)
        self.ledger.before_shell('git commit -m "rename old -> cart.js\n\n- moved cart.js > cart.js"')
        self.assertEqual(self.ledger.edits, 0)
        self.assertEqual(self.ledger.edited, set())
        self.assertEqual(self.ledger.status()[0], "existing")

    def test_a_commit_message_naming_a_new_file_is_not_a_creation(self):
        self.ledger.before_shell('git commit -m "rename old -> gone.js"')
        self.assertEqual(self.ledger.created, set())

    def test_a_write_inside_a_quoted_script_is_still_an_edit(self):
        self.ledger.after_shell("node test.js", 0)
        self.ledger.before_shell("bash -c 'echo x > cart.js'")
        self.assertEqual(self.ledger.edits, 1)
        self.assertEqual(self.ledger.status()[0], "none")

    def test_a_redirect_to_a_quoted_name_edits_that_file(self):
        (self.root / "my file.txt").write_text("old\n")
        self.ledger.after_shell("node test.js", 0)
        self.ledger.before_shell('echo x > "my file.txt"')
        self.assertEqual(self.ledger.edits, 1)
        self.assertEqual(self.ledger.edited, {"my file.txt"})
        self.assertEqual(self.ledger.created, set())
        self.assertEqual(self.ledger.status()[0], "none")

    def test_a_redirect_to_an_escaped_name_edits_that_file(self):
        (self.root / "my file.txt").write_text("old\n")
        self.ledger.after_shell("node test.js", 0)
        self.ledger.before_shell("echo x > my\\ file.txt")
        self.assertEqual(self.ledger.edits, 1)
        self.assertEqual(self.ledger.edited, {"my file.txt"})
        self.assertEqual(self.ledger.created, set())
        self.assertEqual(self.ledger.status()[0], "none")

    def test_an_in_place_edit_of_a_quoted_file_is_an_edit(self):
        for command in ("sed -i 's/a/b/' \"cart.js\"", "perl -pi -e 's/a/b/' \"cart.js\""):
            ledger = evidence.Ledger(self.root, observe_shell=False)
            ledger.after_shell("node test.js", 0)
            ledger.before_shell(command)
            self.assertEqual(ledger.edits, 1, command)
            self.assertEqual(ledger.edited, {"cart.js"}, command)
            self.assertEqual(ledger.status()[0], "none", command)

    def created_new_js(self):
        self.ledger.after_write("write_file", "new.js", {"ok": True})
        (self.root / "new.js").write_text("// new\n")

    def test_a_commit_message_naming_a_new_file_does_not_label_the_check(self):
        self.created_new_js()
        for command in ('git commit -m "add new.js" && node test.js',
                        "git commit -m 'add new.js' && node test.js",
                        'git commit -m "new.js" && node test.js',
                        "bash -c 'git commit -m \"add new.js\" && node test.js'",
                        'echo "wrote new.js" && node test.js',
                        'gh pr create --title "add src/new.js" && node test.js',
                        "git add new.js && node test.js",
                        "node test.js > new.js"):
            self.assertEqual(self.ledger._classify(command), "test", command)

    def test_a_message_naming_a_runner_is_not_a_test(self):
        for command in ('git commit -m "run pytest" && ./cart', './cart --title "run pytest"'):
            self.assertEqual(self.ledger._classify(command), "run", command)

    def test_a_message_naming_a_test_is_not_a_test(self):
        self.assertEqual(self.ledger._classify('git commit -m "test.js" && ./cart'), "run")
        self.ledger.after_write("edit_file", "test.js", {"ok": True, "changed": True})
        self.assertEqual(self.ledger._classify('git commit -m "test.js" && ./cart'), "run")

    def test_running_a_new_file_is_still_its_own(self):
        self.created_new_js()
        (self.root / "my new.js").write_text("// new\n")
        self.ledger.after_write("write_file", "my new.js", {"ok": True})
        for command in ("node new.js", 'node "new.js"', "node ./new.js", "node --import=new.js test.js",
                        'node "my new.js"', 'git commit -m "x" && node new.js',
                        "node -e \"require('./new.js')\"", "bash -c 'node new.js'",
                        'OUT="$(node new.js)" && node test.js'):
            self.assertEqual(self.ledger._classify(command), "own", command)

    def assert_stale(self, command, edited=frozenset(), created=frozenset()):
        ledger = evidence.Ledger(self.root, observe_shell=False)
        ledger.after_shell("node test.js", 0)
        self.assertEqual(ledger.status()[0], "existing", command)
        ledger.before_shell(command)
        ledger.after_shell(command, 0)
        self.assertEqual(ledger.status(), ("none", []), command)
        self.assertEqual(ledger.edited, set(edited), command)
        self.assertEqual(ledger.created, set(created), command)

    def assert_current(self, command, created=frozenset()):
        ledger = evidence.Ledger(self.root, observe_shell=False)
        ledger.after_shell("node test.js", 0)
        ledger.before_shell(command)
        self.assertEqual(ledger.edits, 0, command)
        self.assertEqual(ledger.edited, set(), command)
        self.assertEqual(ledger.created, set(created), command)
        self.assertEqual(ledger.status()[0], "existing", command)

    def test_overwriting_a_file_from_a_heredoc_is_an_edit(self):
        self.assert_stale("cat <<EOF > cart.js\nbody\nEOF\n", edited={"cart.js"})

    def test_cp_and_mv_over_an_existing_file_are_edits(self):
        self.assert_stale("cp test.js cart.js", edited={"cart.js"})
        self.assert_stale("mv test.js cart.js", edited={"test.js", "cart.js"})

    def test_cp_to_a_new_file_is_a_creation(self):
        self.assert_stale("cp cart.js copy.js", created={"copy.js"})

    def test_cp_into_a_directory_edits_the_file_inside_it(self):
        (self.root / "src").mkdir()
        (self.root / "src" / "cart.js").write_text("// old\n")
        for command in ("cp cart.js src", "cp cart.js src/", "cp -t src cart.js", "bash -c 'cp cart.js src'"):
            self.assert_stale(command, edited={"cart.js"})
        self.assert_stale("cp test.js src", created={"test.js"})

    def test_moving_a_file_away_is_an_edit(self):
        self.assert_stale("mv cart.js moved.js", edited={"cart.js"}, created={"moved.js"})

    def test_removing_a_file_is_an_edit(self):
        self.assert_stale("rm cart.js", edited={"cart.js"})

    def test_removing_a_missing_file_is_nothing(self):
        self.assert_current("rm -f gone.js")

    def test_removing_a_scratch_file_this_run_made_does_not_undo_its_check(self):
        command = "cat > scratch.js <<EOF\nx\nEOF\nnode scratch.js"
        self.ledger.before_shell(command)
        (self.root / "scratch.js").write_text("x\n")
        self.ledger.after_shell(command, 0)
        self.ledger.before_shell("rm scratch.js")
        self.assertEqual(self.ledger.edits, 1)
        self.assertEqual(self.ledger.status()[0], "own")

    def test_an_edit_to_unknown_files_makes_earlier_checks_stale(self):
        for command in ("find . -exec sed -i 's/a/b/' {} +", "find . -name '*.js' -exec sed -i 's/a/b/' {} \\;",
                        "find . -exec sh -c 'sed -i s/a/b/ \"$0\"' {} \\;", "find . -exec rm {} +", "find . -delete",
                        "find . -exec cp {} dir/ \\;", "grep -l a . | xargs sed -i 's/a/b/'",
                        "grep -l a . | sudo xargs perl -pi -e 's/a/b/'", "ls | xargs rm -f", "ls | xargs -I{} mv {} old/",
                        "ls | parallel sed -i 's/a/b/'", "sed -i 's/a/b/' *.js",
                        "for f in *.js; do sed -i 's/a/b/' \"$f\"; done", "echo x > \"$OUT\"", "rm src/{a,b}.js",
                        "cp *.js dir/"):
            self.assert_stale(command)

    def test_an_edit_to_unknown_files_keeps_the_ones_it_names(self):
        self.assert_stale("xargs sed -i 's/a/b/' cart.js", edited={"cart.js"})

    def test_reading_unknown_files_is_not_an_edit(self):
        for command in ("find . -name '*.js'", "find . -exec grep a {} +", "find . -exec sed -n '1p' {} +",
                        "ls | xargs grep a", "ls | xargs sed 's/a/b/'", "cat *.js", 'cat "$f"'):
            self.assert_current(command)

    def test_a_comparison_or_a_search_naming_a_file_is_not_an_edit(self):
        for command in ("echo $((a > cart.js))", "[[ a > cart.js ]]", "grep tee cart.js", "grep sed -i cart.js"):
            self.assert_current(command)

    def test_an_edit_after_cd_is_an_edit(self):
        (self.root / "src").mkdir()
        (self.root / "src" / "only.js").write_text("// old\n")
        for command in ("cd src && sed -i 's/a/b/' only.js", "cd src; rm only.js", "cd src && echo x > only.js",
                        "(cd src && rm only.js)", f"cd {self.root / 'src'} && rm only.js", "cd src && touch ../new.js && rm only.js",
                        "cd src && bash -c 'rm only.js'", "git -C src rm only.js", "pushd src && rm only.js"):
            ledger = evidence.Ledger(self.root, observe_shell=False)
            ledger.after_shell("node test.js", 0)
            ledger.before_shell(command)
            self.assertEqual(ledger.status(), ("none", []), command)
            self.assertIn("only.js", ledger.edited, command)
            self.assertNotIn("only.js", ledger.created, command)

    def test_a_cd_keeps_a_removal_away_from_the_file_of_that_name_in_the_root(self):
        (self.root / "src").mkdir()
        self.assert_current("cd src && rm -f cart.js")
        self.assert_stale("cd src && rm -f cart.js; cd .. && rm cart.js", edited={"cart.js"})
        ledger = evidence.Ledger(self.root, observe_shell=False)
        ledger.before_shell("(cd src && rm -f cart.js)")
        self.assertEqual(ledger.edits, 0)
        ledger.before_shell("(cd src && rm -f cart.js); rm cart.js")
        self.assertEqual((ledger.edits, ledger.edited), (1, {"cart.js"}))

    def test_a_cd_to_an_unread_place_makes_earlier_checks_stale(self):
        for command in ('cd "$d" && rm cart.js', "cd - && rm cart.js", 'cd "$d" && sed -i s/a/b/ cart.js'):
            self.assert_stale(command)

    def test_a_cd_out_of_the_project_is_outside_it(self):
        self.assert_current("cd .. && rm -f cart.js")
        self.assert_current("cd .. && echo x > out.log", created={"out.log"})

    def test_a_new_file_through_the_shell_is_an_edit(self):
        for command, name in (("echo x > brand_new.js", "brand_new.js"), ("cat <<EOF > n.js\nbody\nEOF\n", "n.js"),
                              ("echo x | tee n.log", "n.log"), ("cd src && echo x > n.js", "n.js"),
                              ("bash -c 'echo x > n.js'", "n.js")):
            self.assert_stale(command, created={name})

    def test_write_file_and_the_shell_agree_on_a_new_file(self):
        for make in (lambda ledger: ledger.after_write("write_file", "brand_new.js", {"ok": True}),
                     lambda ledger: ledger.before_shell("echo x > brand_new.js")):
            ledger = evidence.Ledger(self.root, observe_shell=False)
            ledger.after_shell("node test.js", 0)
            make(ledger)
            self.assertEqual((ledger.edits, ledger.created), (1, {"brand_new.js"}))

    def test_a_write_outside_the_project_is_not_an_edit(self):
        self.assert_current("echo x > /olith-outside/out.log", created={"out.log"})
        self.assert_current("npm test > /dev/null 2>&1")

    def test_a_check_that_logs_to_a_new_file_stays_current(self):
        for command in ("node test.js > run.log", "node test.js | tee run.log"):
            ledger = evidence.Ledger(self.root, observe_shell=False)
            ledger.before_shell(command)
            ledger.after_shell(command, 0)
            self.assertEqual(ledger.status()[0], "existing", command)

    def test_another_write_to_a_file_this_run_made_is_an_edit(self):
        ledger = evidence.Ledger(self.root, observe_shell=False)
        ledger.before_shell("echo x > again.js")
        (self.root / "again.js").write_text("x\n")
        ledger.after_shell("node test.js", 0)
        self.assertEqual(ledger.status()[0], "existing")
        ledger.before_shell("echo y > again.js")
        self.assertEqual(ledger.status(), ("none", []))
        self.assertEqual((ledger.created, ledger.edited), ({"again.js"}, set()))

    def test_a_shell_write_to_a_file_write_file_made_is_an_edit(self):
        self.created_new_js()
        self.ledger.after_shell("node test.js", 0)
        self.ledger.before_shell("sed -i 's/a/b/' new.js")
        self.assertEqual(self.ledger.status(), ("none", []))
        self.assertEqual((self.ledger.created, self.ledger.edited), ({"new.js"}, set()))

    def test_removing_a_file_write_file_made_keeps_its_check(self):
        self.created_new_js()
        self.ledger.after_shell("node test.js", 0)
        self.ledger.before_shell("rm new.js")
        self.assertEqual(self.ledger.status()[0], "existing")

    def test_a_scratch_file_does_not_cover_for_an_old_file_of_the_same_name(self):
        (self.root / "src").mkdir()
        self.ledger.before_shell("echo x > src/cart.js")
        (self.root / "src" / "cart.js").write_text("x\n")
        self.ledger.after_shell("node test.js", 0)
        self.ledger.before_shell("rm src/cart.js")
        self.assertEqual(self.ledger.status()[0], "existing")
        self.ledger.before_shell("rm cart.js")
        self.assertEqual(self.ledger.status(), ("none", []))
        self.assertIn("cart.js", self.ledger.edited)

    def test_a_symlink_loop_does_not_stop_the_run(self):
        (self.root / "loop.js").symlink_to("loop.js")
        self.assert_current("rm loop.js")
        self.assert_stale("echo x > loop.js", created={"loop.js"})

    def test_git_rm_is_a_removal(self):
        for command in ("git rm cart.js", "git rm -f cart.js", "sudo git rm cart.js", "git -C . rm cart.js"):
            self.assert_stale(command, edited={"cart.js"})
        for command in ("git rm --cached cart.js", "git rm -n cart.js", "git rm --dry-run cart.js", "git rm -f gone.js"):
            self.assert_current(command)
        self.assert_stale("ls | xargs git rm")
        self.assert_stale("ls | xargs git checkout --")

    def test_restoring_files_from_git_makes_earlier_checks_stale(self):
        for command in ("git checkout -- cart.js", "git checkout HEAD~1 -- cart.js", "git checkout .", "git checkout cart.js",
                        "git restore cart.js", "git restore .", "git restore --source=HEAD~1 cart.js", "git restore -s HEAD~1 cart.js",
                        "git restore --staged --worktree cart.js", "git restore -SW cart.js", "git checkout -f",
                        "cd src && git restore cart.js", "sudo git restore cart.js", "git restore '*.js'",
                        'cd "$d" && git restore cart.js'):
            self.assert_stale(command)

    def test_git_commands_that_leave_the_working_tree_alone(self):
        for command in ("git restore --staged cart.js", "git restore -S cart.js", "git checkout -b new", "git checkout -q -b new",
                        "git checkout -qb new", "git checkout -B new", "git checkout --orphan new", "git switch -c new",
                        "git switch --create new", "git switch -C new", "git checkout", "git status", "git add .",
                        "git diff -- cart.js", "git log --oneline -- cart.js", "git branch checkout", "git stash list"):
            self.assert_current(command)

    def test_moving_to_another_branch_makes_earlier_checks_stale(self):
        for command in ("git checkout main", "git checkout -", "git checkout -q main", "git checkout -t origin/main",
                        "git switch main", "git switch -", "git switch --detach main", "git checkout -b new main",
                        "git checkout -qb new main", "git switch -c new main"):
            self.assert_stale(command)

    def test_tests_that_came_back_from_git_are_still_the_projects_own(self):
        for command in ("git checkout -- test.js && node test.js", "git restore test.js && node test.js",
                        "git checkout main && node test.js", "git restore . && python3 -m unittest"):
            ledger = evidence.Ledger(self.root, observe_shell=False)
            ledger.before_shell(command)
            ledger.after_shell(command, 0)
            self.assertEqual(ledger.status()[0], "existing", command)

    def test_touch_only_counts_when_it_makes_a_file(self):
        for command in ("touch cart.js", "touch -d 2020-01-01 cart.js", "touch -r test.js cart.js", "touch -c new.js"):
            self.assert_current(command)
        self.assert_stale("touch new.js", created={"new.js"})
        self.assert_stale("touch -d 2020-01-01 new.js", created={"new.js"})
        self.assert_stale("touch cart.js new.js", created={"new.js"})
        self.assert_stale("cd src && touch new.js", created={"new.js"})
        self.assert_stale("ls | xargs touch")

    def test_ln_install_and_dd_write_their_destination(self):
        (self.root / "src").mkdir()
        (self.root / "src" / "cart.js").write_text("// old\n")
        for command in ("ln -sf test.js cart.js", "ln -f test.js cart.js", "install test.js cart.js", "install -m 644 test.js cart.js",
                        "dd if=test.js of=cart.js", "ln -sf ../x/cart.js", "install -t src cart.js", "ln -sf test.js src/cart.js"):
            self.assert_stale(command, edited={"cart.js"})
        for command in ("ln -s cart.js link.js", "install cart.js link.js", "dd if=cart.js of=link.js"):
            self.assert_stale(command, created={"link.js"})
        for command in ("install -d newdir", "dd if=cart.js", "dd if=cart.js of=/dev/null", "grep touch cart.js", "echo install cart.js"):
            self.assert_current(command)

    def assert_label(self, edit, label, runs=("python3 -m unittest", "npm test", "node test.js")):
        for run in runs:
            ledger = evidence.Ledger(self.root, observe_shell=False)
            ledger.before_shell(edit)
            ledger.after_shell(run, 0)
            self.assertEqual(ledger.status()[0], label, f"{edit} then {run}")
            self.assertEqual(ledger._classify(run), {"own": "own", "existing": "test"}[label], f"{edit} then {run}")

    def test_an_edit_that_could_reach_any_file_means_tests_may_have_changed(self):
        for command in ("find . -exec sed -i 's/a/b/' {} +", "find . -name '*.js' -delete", "ls | xargs sed -i 's/a/b/'",
                        "ls | xargs rm", "ls | parallel sed -i 's/a/b/'", "for f in *.js; do sed -i 's/a/b/' \"$f\"; done",
                        "rm src/{a,b}.js", 'cd "$d" && sed -i s/a/b/ cart.js'):
            self.assert_label(command, "own")

    def test_a_test_shaped_pattern_means_tests_changed(self):
        for command in ("sed -i 's/a/b/' tests/*.py", "rm test_*.py", "sed -i 's/a/b/' src/*.test.js", "cp *.js tests/",
                        "sed -i 's/a/b/' *_test.go", "cd tests && sed -i 's/a/b/' *.py"):
            self.assert_label(command, "own")

    def test_a_glob_is_read_off_the_disk(self):
        self.assert_label("sed -i 's/a/b/' *.js", "own")
        (self.root / "src").mkdir()
        (self.root / "src" / "cart.js").write_text("// old\n")
        (self.root / "a.log").write_text("log\n")
        for command in ("rm -f *.log", "rm -f nothing/*.js", "sed -i 's/a/b/' src/*.js", "cd src && sed -i 's/a/b/' *.js",
                        "cp *.js dist/"):
            self.assert_label(command, "existing")
            self.assert_stale(command)
        (self.root / "src" / "cart.test.js").write_text("// test\n")
        self.assert_label("sed -i 's/a/b/' src/*.js", "own")
        self.assert_label("cd src && sed -i 's/a/b/' *.js", "own")
        self.assert_label("mv * old/", "own")

    def test_a_folder_of_tests_caught_by_a_glob_counts(self):
        (self.root / "tests").mkdir()
        self.assert_label("rm -rf test?", "own")

    def test_a_discovered_test_command_is_own_after_an_unknown_edit(self):
        report = {"commands": [{"command": "npm test", "kind": "test", "result": {"exit_code": 0}}]}
        self.ledger.before_shell("ls | xargs sed -i 's/a/b/'")
        self.assertEqual(self.ledger.status(report)[0], "own")

    def test_a_run_that_is_not_a_test_stays_a_run_after_an_unknown_edit(self):
        self.ledger.before_shell("ls | xargs sed -i 's/a/b/'")
        self.ledger.after_shell("g++ -o cart cart.cpp && ./cart", 0)
        self.assertEqual(self.ledger.status()[0], "ran")

    def test_a_test_behind_a_wrapper_is_labelled_by_what_it_ran(self):
        self.assertEqual(self.ledger._classify("sudo node test.js"), "test")
        self.assertEqual(self.ledger._classify("sudo cat test.js && ./cart"), "run")

    def test_unbacked_claims_are_caught_and_backed_ones_are_not(self):
        self.edit()
        self.assertIsNotNone(self.ledger.claim_problem("All fixed and verified."))
        self.assertIsNone(self.ledger.claim_problem("Fixed it, but I could not test it."))
        self.ledger.after_shell("node test.js", 1)
        self.assertIn("still fails", self.ledger.claim_problem("All tests pass."))
        self.ledger.after_shell("node test.js", 0)
        self.assertIsNone(self.ledger.claim_problem("All tests pass."))

    def test_no_edits_means_nothing_to_contradict(self):
        self.assertIsNone(evidence.Ledger(self.root, observe_shell=False).claim_problem("Verified."))

    def test_a_failing_check_contradicts_a_claim_even_without_edits(self):
        ledger = evidence.Ledger(self.root, observe_shell=False)
        ledger.after_shell("node test.js", 1)
        self.assertIn("still fails", ledger.claim_problem("All tests pass."))

    def test_a_failing_automatic_check_counts(self):
        self.edit()
        report = {"commands": [{"command": "npm test", "result": {"exit_code": 1}}]}
        self.assertEqual(self.ledger.status(report)[0], "failing")


class ChangeSummary(unittest.TestCase):
    BEFORE = "int score = 0;\n\nvoid step(int dt) {\n    score += dt * 0;\n}\n\nvoid draw() {\n    paint();\n}\n"

    def test_changed_definitions_are_named(self):
        after = self.BEFORE.replace("dt * 0", "dt * 12")
        changed = evidence.changed_symbols({"files": [{"file": "game.cpp", "status": "modified", "before_text": self.BEFORE, "after_text": after}]})
        self.assertEqual(changed, {"game.cpp": ["step"]})

    def test_reindenting_is_not_a_change(self):
        after = self.BEFORE.replace("    ", "        ").replace("dt * 0", "dt * 12")
        changed = evidence.changed_symbols({"files": [{"file": "game.cpp", "status": "modified", "before_text": self.BEFORE, "after_text": after}]})
        self.assertEqual(changed, {"game.cpp": ["step"]})

    def test_debrief_must_mention_what_changed(self):
        changed = {"game.cpp": ["step", "draw"]}
        self.assertEqual(evidence.unmentioned("Fixed the score in step().", changed), ["draw"])
        self.assertEqual(evidence.unmentioned("Changed step and draw.", changed), [])

    def test_sweeping_rewrites_are_not_itemized(self):
        changed = {"a.py": [f"f{i}" for i in range(20)]}
        self.assertEqual(evidence.unmentioned("Rewrote a.py.", changed), [])

    def test_the_record_says_where_each_check_came_from(self):
        checks = [{"command": "node test.js", "exit_code": 0, "kind": "test"},
                  {"command": "./test_cart", "exit_code": 0, "kind": "own"},
                  {"command": "g++ game.cpp", "exit_code": 0, "kind": "run", "auto": True}]
        text = evidence.block("existing", checks, {"cart.js": ["subtotal"]})
        self.assertIn("✓ `node test.js` (existing test)", text)
        self.assertIn("✓ `g++ game.cpp` (automatic, build or run)", text)
        self.assertIn("✓ `./test_cart` (written this run)", text)
        self.assertIn("Changed: cart.js (subtotal)", text)
        self.assertIn("Only checked with tests Olith wrote", evidence.block("own", checks[1:2], None))
        self.assertIn("no existing test checked the behavior", evidence.block("ran", checks[2:], None))


if __name__ == "__main__":
    unittest.main()


class ObservedShellChanges(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        (self.root / "test.js").write_text("// existing test\n")
        (self.root / "cart.js").write_text("// source\n")
        self.ledger = evidence.Ledger(self.root)
        self.ledger.after_shell("node test.js", 0)

    def run_shell(self, command):
        self.ledger.before_shell(command)
        result = subprocess.run(command, shell=True, cwd=self.root, capture_output=True, text=True)
        self.ledger.after_shell(command, result.returncode)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_interpreter_changes_invalidate_previous_checks(self):
        command = shlex.quote(sys.executable) + " -c " + shlex.quote("from pathlib import Path; Path('cart.js').write_text('// edited')")
        self.run_shell(command)
        self.assertNotEqual(self.ledger.status()[0], "existing")
        self.assertGreater(self.ledger.edits, 0)

    def test_failed_command_without_changes_preserves_checks(self):
        self.ledger.before_shell("sed -i missing.js")
        self.ledger.after_shell("sed -i missing.js", 1)
        self.assertEqual(self.ledger.status()[0], "existing")

    def test_cd_edits_and_subshell_removals_are_observed(self):
        (self.root / "src").mkdir()
        (self.root / "src/cart.js").write_text("a")
        self.run_shell("cd src && sed -i 's/a/b/' cart.js")
        self.assertEqual(self.ledger.status()[0], "none")
        self.ledger.after_shell("node test.js", 0)
        self.run_shell("(cd src && rm cart.js)")
        self.assertEqual(self.ledger.status()[0], "none")

    def test_touch_and_identical_rewrites_preserve_checks(self):
        self.run_shell("touch cart.js")
        self.run_shell("printf '// source\\n' > cart.js")
        self.assertEqual(self.ledger.status()[0], "existing")
        self.assertEqual(self.ledger.edits, 0)

    def test_new_files_and_second_writes_invalidate_checks(self):
        self.run_shell("echo x > again.js")
        self.assertEqual(self.ledger.status()[0], "none")
        self.ledger.after_shell("node test.js", 0)
        self.run_shell("echo y > again.js")
        self.assertEqual(self.ledger.status()[0], "none")
        self.assertEqual(self.ledger.edits, 2)

    def test_removing_own_scratch_file_preserves_checks(self):
        self.run_shell("echo x > scratch.js")
        self.ledger.after_shell("node test.js", 0)
        self.run_shell("rm scratch.js")
        self.assertEqual(self.ledger.status()[0], "existing")

    def test_test_folder_helpers_change_runner_provenance(self):
        self.run_shell("mkdir tests && echo x > tests/helper.js")
        self.ledger.after_shell("npm test", 0)
        self.assertEqual(self.ledger.status()[0], "own")

    def test_non_test_globs_do_not_mark_tests_as_own(self):
        (self.root / "src").mkdir()
        (self.root / "src/cart.js").write_text("a")
        self.run_shell("find src -name '*.js' -exec sed -i 's/a/b/' {} +")
        self.ledger.after_shell("npm test", 0)
        self.assertEqual(self.ledger.status()[0], "existing")

    def test_piped_test_output_does_not_become_a_test(self):
        self.run_shell("true | tee test.log")
        self.ledger.after_shell("npm test | tee test.log", 0)
        self.assertEqual(self.ledger.status()[0], "existing")

    def test_ignored_dependencies_and_build_output_preserve_checks(self):
        self.run_shell("mkdir -p node_modules/pkg build && echo x > node_modules/pkg/index.js && echo y > build/out.js")
        self.assertEqual(self.ledger.status()[0], "existing")

    def test_binary_and_extensionless_project_files_are_observed(self):
        self.run_shell("printf x > fixture.bin")
        self.assertEqual(self.ledger.status()[0], "none")
        self.ledger.after_shell("node test.js", 0)
        self.run_shell("printf x > resource")
        self.assertEqual(self.ledger.status()[0], "none")

    def test_same_size_rewrite_with_restored_mtime_is_observed(self):
        path = self.root / "cart.js"
        stamp = path.stat()
        self.ledger.before_shell("custom-tool")
        path.write_text("// change\n")
        os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
        self.ledger.after_shell("custom-tool", None)
        self.assertEqual(self.ledger.status()[0], "none")

    def test_timeout_still_observes_partial_writes(self):
        self.ledger.before_shell("long-running-tool")
        (self.root / "cart.js").write_text("// partial")
        self.ledger.after_shell("long-running-tool", None)
        self.assertEqual(self.ledger.status()[0], "none")

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.root, check=True, capture_output=True, text=True)

    def test_git_reset_stash_restore_apply_and_clean_use_real_changes(self):
        self.git("init", "-b", "main")
        self.git("config", "user.name", "Test")
        self.git("config", "user.email", "test@example.invalid")
        self.git("config", "commit.gpgsign", "false")
        self.git("add", ".")
        self.git("commit", "-m", "Initial")
        for command in ("git reset --hard HEAD", "git stash push", "git restore cart.js"):
            with self.subTest(command=command):
                (self.root / "cart.js").write_text("// dirty\n")
                self.ledger.after_shell("node test.js", 0)
                self.run_shell(command)
                self.assertEqual(self.ledger.status()[0], "none")
        self.ledger.after_shell("node test.js", 0)
        self.run_shell("git stash pop")
        self.assertEqual(self.ledger.status()[0], "none")
        self.git("restore", "cart.js")
        self.ledger.after_shell("node test.js", 0)
        self.run_shell("git reset --hard HEAD")
        self.assertEqual(self.ledger.status()[0], "existing")
        (self.root / "untracked.js").write_text("// user file")
        self.ledger.after_shell("node test.js", 0)
        self.run_shell("git clean -f")
        self.assertEqual(self.ledger.status()[0], "none")

    def test_git_noop_checkout_keeps_existing_check(self):
        self.git("init", "-b", "main")
        self.git("config", "user.name", "Test")
        self.git("config", "user.email", "test@example.invalid")
        self.git("config", "commit.gpgsign", "false")
        self.git("add", ".")
        self.git("commit", "-m", "Initial")
        self.run_shell("git checkout main")
        self.assertEqual(self.ledger.status()[0], "existing")

    def test_new_test_created_and_run_in_one_command_is_own(self):
        command = "printf 'pass\n' > test_new.py && " + shlex.quote(sys.executable) + " test_new.py"
        self.run_shell(command)
        self.assertEqual(self.ledger.status()[0], "own")

    def test_test_in_cd_folder_is_recognized_and_new_test_is_own(self):
        (self.root / "src").mkdir()
        (self.root / "src/test_src.py").write_text("pass\n")
        self.run_shell("cd src && " + shlex.quote(sys.executable) + " test_src.py")
        self.assertEqual(self.ledger.status()[0], "existing")
        self.run_shell("echo '# changed' >> src/test_src.py")
        self.run_shell("cd src && " + shlex.quote(sys.executable) + " test_src.py")
        self.assertEqual(self.ledger.status()[0], "own")

    def test_failed_mutation_stales_checks_even_when_command_fails(self):
        command = "echo partial > cart.js; false"
        self.ledger.before_shell(command)
        result = subprocess.run(command, cwd=self.root, shell=True, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.ledger.after_shell(command, result.returncode)
        self.assertEqual(self.ledger.status()[0], "none")

    def test_incomplete_snapshots_cannot_certify_a_check(self):
        import task_changes
        for limit, value in (("MAX_SNAPSHOT_BYTES", 1), ("MAX_FILES", 1)):
            with self.subTest(limit=limit), patch.object(task_changes, limit, value):
                self.run_shell("echo read-only")
                self.assertEqual(self.ledger.status()[0], "none")
                self.assertFalse(self.ledger.snapshot_complete)
                self.ledger.before_shell("node test.js")
                self.ledger.after_shell("node test.js", 0)
                self.assertEqual(self.ledger.status()[0], "none")

    def test_automatic_results_are_discarded_after_later_edits(self):
        self.ledger.checks.clear()
        self.ledger.before_shell("node test.js")
        self.ledger.after_shell("node test.js", 0, record_check=False)
        report = {"commands": [{"command": "node test.js", "kind": "test", "result": {"exit_code": 0}, "after_edit": self.ledger.edits}]}
        self.assertEqual(self.ledger.status(report)[0], "existing")
        self.assertEqual(len(self.ledger.status(report)[1]), 1)
        self.run_shell("echo changed > cart.js")
        self.assertEqual(self.ledger.status(report)[0], "none")
        report["commands"][0].update(after_edit=self.ledger.edits, observed=False)
        self.assertEqual(self.ledger.status(report)[0], "none")

    def test_replacing_source_with_symlink_is_observed_without_reading_target(self):
        outside = self.root.parent / (self.root.name + "-outside.txt")
        outside.write_text("outside")
        self.addCleanup(outside.unlink)
        self.ledger.before_shell("replace-with-link")
        (self.root / "cart.js").unlink()
        (self.root / "cart.js").symlink_to(outside)
        self.ledger.after_shell("replace-with-link", None)
        self.assertEqual(self.ledger.status()[0], "none")
        self.assertEqual(outside.read_text(), "outside")

    def test_git_and_custom_ignores_preserve_checks(self):
        self.git("init")
        (self.root / ".gitignore").write_text("output/\n")
        (self.root / ".olithignore").write_text("scratch/\n")
        self.run_shell("mkdir -p output scratch && echo x > output/new.js && echo x > scratch/new.js")
        self.assertEqual(self.ledger.status()[0], "existing")

    def test_git_apply_and_branch_changes_invalidate_checks(self):
        self.git("init", "-b", "main")
        self.git("config", "user.name", "Test")
        self.git("config", "user.email", "test@example.invalid")
        self.git("config", "commit.gpgsign", "false")
        self.git("add", ".")
        self.git("commit", "-m", "Initial")
        (self.root / "cart.js").write_text("// next source\n")
        diff = self.git("diff").stdout
        self.git("restore", "cart.js")
        (self.root / "change.patch").write_text(diff)
        self.run_shell("git apply change.patch")
        self.assertEqual(self.ledger.status()[0], "none")
        self.git("add", "cart.js")
        self.git("commit", "-m", "Change source")
        self.git("branch", "old", "HEAD~1")
        self.ledger.after_shell("node test.js", 0)
        self.run_shell("git switch old")
        self.assertEqual(self.ledger.status()[0], "none")
