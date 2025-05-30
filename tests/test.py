import configparser
import os
import subprocess
import sys
import unittest
import time
import tempfile
from typing import Tuple
import shutil
import packaging.version
from importlib import metadata
import warnings

import pikepdf

"""
Those tests are using Dogtail https://gitlab.com/dogtail/dogtail

Other tools using dogtail where you can get example on how to use it:

* https://gitlab.gnome.org/GNOME/gnome-boxes
* https://gitlab.gnome.org/GNOME/gnome-software
* https://gitlab.gnome.org/GNOME/gnome-characters
* https://gitlab.gnome.org/GNOME/eog
* https://github.com/kirienko/gourmet
* https://github.com/virt-manager/virt-manager
* https://gitlab.gnome.org/GNOME/gnome-weather/
* http://threedepict.sourceforge.net/
* https://salsa.debian.org/pkg-privacy-team/onioncircuits/-/tree/master/debian/tests

Example of Dogtail using a filechooser:

filechooser = app.child(roleName='file chooser')
treeview = filechooser.child(roleName='table', name='Files')
treeview.keyCombo('<ctrl>L')
treeview.typeText('qpdf-manual.pdf')
filechooser.button('Open').click()

You may need to run the following commands to run those tests in your current session instead of Xvfb:

* /usr/libexec/at-spi-bus-launcher --launch-immediately
* setxkbmap -v fr
* gsettings set org.gnome.desktop.interface toolkit-accessibility true

Tests need to be run with a window size of 545x600. It must be set in ~/.config/pdfarranger/config.ini.

Some tips:

* Use to print widget tree (names and roles) self._app().dump()

Example of how to run the test locally:
python3 -X tracemalloc -u -m unittest -v -f tests.test # run whole test
python3 -X tracemalloc -u -m unittest -v -f tests.test.TestBatch5 # run only TestBatch5
"""


def group(title):
    if "GITHUB_ACTIONS" in os.environ:
        print("::group::" + title)


def endgroup():
    if "GITHUB_ACTIONS" in os.environ:
        print("::endgroup::")


def check_img2pdf(version):
    try:
        import img2pdf
        v = [int(x) for x in img2pdf.__version__.split(".")]
        r = v >= version
    except Exception:
        r = False
    return r


def have_pikepdf8():
    return packaging.version.parse(
        metadata.version("pikepdf")
    ) >= packaging.version.Version("8.0")


class XvfbManager:
    """Base class for running offscreen tests"""

    def __init__(self, display=":99"):
        self.display = display
        env = os.environ.copy()
        env["DISPLAY"] = display
        self.xvfb_proc = None
        self.dbus_proc = None
        self.xvfb_proc = subprocess.Popen(["Xvfb", self.display])
        cmd = ["dbus-daemon", "--print-address=1", "--session"]
        self.dbus_proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, text=True, env=env
        )
        self.dbus_addr = self.dbus_proc.stdout.readline().strip()
        self.dbus_proc.stdout.close()
        os.environ["DISPLAY"] = display
        os.environ["DBUS_SESSION_BUS_ADDRESS"] = self.dbus_addr

    def kill(self):
        if "GITHUB_ACTIONS" not in os.environ:
            # Workaround. On GHA killing dbus also kill this python process.
            # And we don't care about zombie processes on GHA
            self.dbus_proc.kill()
            self.dbus_proc.wait()
        self.xvfb_proc.kill()
        self.xvfb_proc.wait()


class DogtailManager:
    def __init__(self):
        self.xvfb = None
        if "DISPLAY" not in os.environ:
            self.xvfb = XvfbManager()
        os.environ["LC_MESSAGES"] = "C"
        os.environ["GTK_MODULES"] = "gail:atk-bridge"
        cmd = "gsettings set org.gnome.desktop.interface toolkit-accessibility true"
        subprocess.check_call(cmd.split())
        # dogtail must be imported after setting DBUS_SESSION_BUS_ADDRESS
        from dogtail.config import config
        config.debugSleep = False
        # long duration at startup
        config.searchBackoffDuration = 1
        config.actionDelay = 0.01
        config.runInterval = 0.01
        config.defaultDelay = 0.1
        config.debugSearching = False
        config.searchCutoffCount = 30
        config.runTimeout = 1
        config.searchShowingOnly = True
        config.typingDelay = 0.05

    def kill(self):
        if self.xvfb is not None:
            self.xvfb.kill()


# dogtail does not support change of X11 server so it must be a singleton
dogtail_manager = DogtailManager()

def setUpModule():
    # if we can't kill the dbus process on GHA, let's at least suppress the warning
    if dogtail_manager.xvfb and "GITHUB_ACTIONS" in os.environ:
        warnings.filterwarnings("ignore",
                                f"subprocess {dogtail_manager.xvfb.dbus_proc.pid} is still running",
                                ResourceWarning)

def tearDownModule():
    dogtail_manager.kill()


class PdfArrangerManager:
    def __init__(self, args=None):
        self.process = None
        args = [] if args is None else args
        cmd = [sys.executable, "-u", "-X", "tracemalloc"]
        if "PDFARRANGER_COVERAGE" in os.environ:
            cmd = cmd + ["-m", "coverage", "run", "--concurrency=thread,multiprocessing", "-a"]
        self.process = subprocess.Popen(cmd + ["-m", "pdfarranger"] + args, stdout=subprocess.PIPE)

    def _get_stdout(self):
        """get the stdout of the pdfarranger process"""
        return self.process.stdout.read().decode("utf-8")

    def kill(self):
        self.process.stdout.close()
        self.process.kill()
        self.process.wait()

class PdfArrangerTest(unittest.TestCase):
    def _start(self, args=None, gtk_check=True):
        from dogtail.config import config
        config.searchBackoffDuration = 1
        self.__class__.pdfarranger = PdfArrangerManager(args)
        if not gtk_check:
            return
        # check that process is actually running
        self.assertIsNone(self._process().poll())
        self._app()
        # Now let's go faster
        config.searchBackoffDuration = 0.1

    def _get_stdout(self):
        """get the stdout of the pdfarranger process"""
        return self.__class__.pdfarranger._get_stdout()

    def _app(self):
        """Return the first instance of pdfarranger"""
        self._wait_cond(lambda: len(self._apps()) > 0)
        a =  self._apps()[0]
        self.assertFalse(a.dead)
        return a

    def _apps(self):
        """Return all instances of pdfarranger"""
        # Cannot import at top level because of DBUS_SESSION_BUS_ADDRESS
        from dogtail.tree import root

        return [
            a
            for a in root.applications()
            if a.name in ["__main__.py", "pdfarranger", "com.github.jeromerobert.pdfarranger"]
        ]

    def _mainmenu(self, action):
        mainmenu = self._app().child(roleName="toggle button", name="Menu")
        self._wait_cond(lambda: mainmenu.sensitive)
        mainmenu.click()
        if not isinstance(action, str):
            for submenu in action[:-1]:
                mainmenu.menu(submenu).point()
            action = action[-1]
        mainmenu.menuItem(action).click()

    def _wait_cond(self, cond):
        c = 0
        while not cond():
            time.sleep(0.1)
            self.assertLess(c, 300)
            c += 1

    def _find_by_role(self, role, node=None, show_only=False):
        if node is None:
            node = self._app()
        from dogtail import predicate

        return node.findChildren(
            predicate.GenericPredicate(roleName=role), showingOnly=show_only
        )

    def _is_saving(self):
        allstatusbar = self._find_by_role("status bar")
        statusbar = allstatusbar[-1]
        return statusbar.name.startswith("Saving")

    def _wait_saving(self):
        # When saving the main window is made unresponsive. When saving end
        # it's made responsive again. We must be sure that it's responsive
        # before continuing test else clicks may fail.
        self._wait_cond(lambda: not self._is_saving())

    def _status_text(self):
        self._app()
        allstatusbar = self._find_by_role("status bar")
        # If we have multiple status bar, consider the first one as the one who display the selection
        statusbar = allstatusbar[0]
        return statusbar.name

    def _assert_selected(self, selection):
        self.assertTrue(self._status_text().startswith("Selected pages: " + selection))

    def _assert_page_size(self, width, height, pageid=None):
        if pageid is not None:
            self._icons()[pageid].click()
            self._wait_cond(lambda: self._status_text().startswith(f"Selected pages: {pageid+1}"))
        label = " {:.1f} mm \u00D7 {:.1f} mm".format(width, height)
        self.assertTrue(self._status_text().endswith("Page Size:" + label))

    def _check_file_content(self, filename, expected: Tuple[str]) -> Tuple[bool, str]:
        """
        Check expected is contained in file.
        """
        with open(filename, 'rb') as f:
            actual = f.readlines()

        n = 0
        for line in expected:
            try:
                while not actual[n].startswith(line):
                    n += 1
                n += 1
            except IndexError: # pragma: no cover
                # Only get executed for failing test
                return False, line
        return True, ''

    def _assert_page_content(self, filename: str, expected: Tuple[str]):
        """
        Check if a file in the current tmp folder contains expected content.
        """
        temp = os.path.join(self.__class__.tmp, 'temp.pdf')
        with pikepdf.Pdf.open(os.path.join(self.__class__.tmp, filename)) as pdf:
            pdf.save(temp, qdf=True, static_id=True, compress_streams=False,
                            stream_decode_level=pikepdf.StreamDecodeLevel.all)
        ok, content = self._check_file_content(temp, expected)
        self.assertTrue(ok, f'expectent content {content} missing in {filename}')

    def _assert_file_size(self, filename, size, tolerance=0.03):
        """
        Check if a file in the current tmp folder have the expected size
        at a given tolerance.
        """
        s = os.stat(os.path.join(self.__class__.tmp, filename))
        msg = "{} is {}b but is expected to be {} \u00B1 {}".format(
            filename, s.st_size, size, size * tolerance
        )
        self.assertLess(abs(s.st_size - size) / size, tolerance, msg=msg)

    def _icons(self):
        """Return the list of page icons"""
        viewport = self._app().child(roleName="layered pane")
        return self._find_by_role("icon", viewport)

    def _popupmenu(self, page, action):
        """Run an action on a give page using the popup menu"""
        if page is None:
            self._app().keyCombo("<shift>F10")
        else:
            self._icons()[page].click(button=3)
        popupmenu = self._app().child(roleName="window")
        if not isinstance(action, str):
            for submenu in action[:-1]:
                popupmenu.menu(submenu).point()
            action = action[-1]
        button = popupmenu.menuItem(action)
        self._wait_cond(lambda: button.sensitive)
        button.click()

    def _process(self):
        return self.__class__.pdfarranger.process

    def _import_file(self, filename, open_action=False):
        """Try to import a file with a file chooser and return that file chooser object"""
        self._mainmenu("Open" if open_action else "Import")
        filechooser = self._app().child(roleName='file chooser')
        treeview = filechooser.child(roleName="table", name="Files")
        treeview.keyCombo("<ctrl>L")
        treeview.typeText(os.path.abspath(filename))
        ob = filechooser.button("Open")
        self._wait_cond(lambda: ob.sensitive)
        ob.click()
        return filechooser

    def _save_as_chooser(self, filebasename, expected=None):
        """
        Fill and validate a Save As file chooser.
        The file chooser is supposed to be already open.
        """
        if expected is None:
            expected = [filebasename]
        filechooser = self._app().child(roleName="file chooser")
        tmp = self.__class__.tmp
        filename = os.path.join(tmp, filebasename)
        filechooser.child(roleName="text").text = filename
        saveb = filechooser.button("Save")
        self._wait_cond(lambda: saveb.sensitive)
        filechooser.button("Save").click()
        # Check files have been created
        for e in expected:
            self._wait_cond(lambda: os.path.isfile(os.path.join(tmp, e)))
        self._wait_cond(lambda: filechooser.dead)
        self._wait_saving()

    def _scale_selected(self, scale):
        app = self._app()
        app.keyCombo("S")
        dialog = app.child(roleName="dialog")
        relative = self._find_by_role("radio button", dialog)[1]
        relative.click()
        from dogtail import rawinput
        rawinput.keyCombo("Tab")
        rawinput.typeText(str(scale))
        dialog.child(name="OK").click()
        self._wait_cond(lambda: dialog.dead)

    @staticmethod
    def _zoom(widget, n_events, zoom_in):
        """Zoom in/out with ctrl + mouse scroll wheel"""
        from dogtail import rawinput
        button = 4 if zoom_in else 5
        rawinput.holdKey("Control_L")
        for __ in range(n_events):
            widget.click(button=button)
        rawinput.releaseKey("Control_L")

    def _config(self):
        """Get config from config file used in test"""
        config = configparser.ConfigParser()
        config.read(self.__class__.config_file)
        return config

    def _quit(self):
        self._app().child(roleName="layered pane").grabFocus()
        self._app().child(roleName="layered pane").keyCombo("<ctrl>q")

    def _quit_without_saving(self):
        self._quit()
        dialog = self._app().child(roleName="alert")
        dialog.child(name="Don’t Save").click()
        # check that process actually exit
        self._process().wait(timeout=22)

    default_config = """
                     [preferences]
                     content-loss-warning = False
                     """

    @classmethod
    def setUpClass(cls):
        cls.pdfarranger = None
        cls.tmp = tempfile.mkdtemp()
        os.environ["APPDATA"] = cls.tmp
        os.makedirs(os.path.join(cls.tmp, "pdfarranger"))
        cls.config_file = os.path.join(os.environ["APPDATA"], "pdfarranger", "config.ini")
        config = configparser.ConfigParser()
        config.read_string(cls.default_config)
        if hasattr(cls, "test_config"):
            config.read_string(cls.test_config)
        with open(cls.config_file, "w") as f:
            config.write(f)

    def setUp(self):
        group("Running " + self.id())

    def tearDown(self):
        endgroup()

    @classmethod
    def tearDownClass(cls):
        if cls.pdfarranger:
            cls.pdfarranger.kill()
        if cls.tmp:
            shutil.rmtree(cls.tmp)


def with_config(test_config):
    """Decorator for test batch specific configuration settings

    test_config is the config.ini content to be applied on top of the default
    config defined in PdfArrangerTest.default_config
    """
    def f(cls):
        cls.test_config = test_config
        return cls
    return f


@with_config("""[preferences]
                content-loss-warning=True""")
class TestBatch1(PdfArrangerTest):
    def test_01_import_img(self):
        self._start(["data/screenshot.png"])
        # Handle the "content loss warning" dialog
        dialog = self._find_by_role("dialog")[-1]
        check_box = self._find_by_role("check box", dialog)[-1]
        check_box.click()  # Don't show this dialog again
        dialog.button("OK").click()
        self._wait_cond(lambda: dialog.dead)

    def test_02_properties(self):
        self._mainmenu("Edit Properties")
        dialog = self._app().child(roleName="dialog")
        creatorlab = dialog.child(roleName="table cell", name="Creator")
        creatorid = creatorlab.parent.children.index(creatorlab) + 1
        creatorval = creatorlab.parent.children[creatorid]
        creatorval.keyCombo("enter")
        from dogtail import rawinput
        rawinput.typeText('["Frodo", "Sam"]')
        dialog.child(name="OK").click()
        self._mainmenu("Edit Properties")
        dialog = self._app().child(roleName="dialog")
        rawinput.keyCombo("enter")
        rawinput.typeText('Memories')
        rawinput.keyCombo("enter")
	# FIXME: depending on where the test is ran the previous enter close
	# the dialog or do not close it.
        try:
            dialog.child(name="OK").click()
        except Exception:
            print("'Edit Properties dialog' closed by 'enter'.")
        self._wait_cond(lambda: dialog.dead)

    def test_03_zoom(self):
        app = self._app()
        zoomoutb = app.child(description="Zoom Out")
        zoominb = app.child(description="Zoom In")
        # maximum dezoom whatever the initial zoom level
        for _ in range(10):
            zoomoutb.click()
        for _ in range(3):
            zoominb.click()

    def test_04_rotate_undo(self):
        app = self._app()
        self._assert_selected("")
        app.keyCombo("<ctrl>a")  # select all
        self._assert_selected("1")
        app.keyCombo("<ctrl>Left")  # rotate left
        app.keyCombo("<ctrl>z")  # undo
        app.keyCombo("<ctrl>y")  # redo
        app.keyCombo("<ctrl>a")
        app.keyCombo("<ctrl>Right")  # rotate right
        app.keyCombo("<ctrl>Right")  # rotate right

    def test_05_duplicate(self):
        self._popupmenu(0, "Duplicate")
        app = self._app()
        self.assertEqual(len(self._icons()), 2)
        self._app().child(roleName="layered pane").grabFocus()
        app.keyCombo("<ctrl>a")
        app.keyCombo("<ctrl>c")
        for __ in range(3):
            app.keyCombo("<ctrl>v")
        self.assertEqual(len(self._icons()), 8)
        app.keyCombo("Right")
        app.keyCombo("Left")
        app.keyCombo("Down")
        self._assert_selected("4")
        app.keyCombo("Up")
        self._assert_selected("2")

    def test_06_crop_margins(self):
        self._popupmenu(None, ["Select", "Select Odd Pages"])
        self._assert_selected("1, 3, 5, 7")
        self._popupmenu(0, "Crop Margins…")
        dialog = self._app().child(roleName="dialog")
        dialog.child(name="Show values").click()
        time.sleep(0.2)  # Avoid 'GTK_IS_RANGE (range)' failed
        croppanel = dialog.child(name="Crop Margins")
        cropbuttons = self._find_by_role("spin button", croppanel)
        for i in range(4):
            cropbuttons[i].click()
            cropbuttons[i].text = str((i+1)*4)
        dialog.child(name="OK").click()
        # TODO: find the condition which could replace this ugly sleep
        time.sleep(0.5)
        self._wait_cond(lambda: dialog.dead)

    def test_07_split_page(self):
        lp = self._app().child(roleName="layered pane")
        lp.grabFocus()
        lbefore = len(self._icons())
        self._popupmenu(0, ["Select", "Select Even Pages"])
        self._assert_selected("2, 4, 6, 8")
        self._mainmenu(["Edit", "Split Pages…"])
        dialog = self._app().child(roleName="dialog")
        dialog.child(name="OK").click()
        self._wait_cond(lambda: dialog.dead)
        self.assertEqual(len(self._icons()), lbefore + 4)

    def test_08_zoom_pages(self):
        self._app().child(roleName="layered pane").keyCombo("Home")
        self._assert_selected("1")
        self._app().keyCombo("f")
        for __ in range(2):
            self._app().keyCombo("minus")
        # Zoom level is now 0 and that's what will be saved to config.ini and
        # used by next batches

    def test_09_save_as(self):
        self._mainmenu("Save")
        self._save_as_chooser("foobar.pdf")

    def test_10_reverse(self):
        self._popupmenu(0, ["Select", "Same Page Format"])
        self._assert_selected("1, 4, 7, 10")
        self._popupmenu(0, ["Select", "All From Same File"])
        self._assert_selected("1-12")
        self._popupmenu(0, "Reverse Order")

    def test_11_quit(self):
        self._mainmenu("Quit")
        dialog = self._app().child(roleName="alert")
        dialog.child(name="Cancel").click()
        self._app().keyCombo("<ctrl>s")
        self._wait_saving()
        self._quit()
        # check that process actually exit
        self._process().wait(timeout=22)
        self.assertEqual(self._config()["preferences"]["content-loss-warning"], "False")


class TestBatch2(PdfArrangerTest):
    def test_01_open_empty(self):
        self._start()

    def test_02_import(self):
        filechooser = self._import_file("tests/test.pdf")
        self._wait_cond(lambda: filechooser.dead)
        self.assertEqual(len(self._icons()), 2)

    def test_03_cropborder(self):
        self._popupmenu(0, "Crop White Borders")

    def test_04_past_overlay(self):
        app = self._app()
        app.keyCombo("<ctrl>c")
        app.keyCombo("Right")
        app.keyCombo("<shift><ctrl>o")
        dialog = self._app().child(roleName="dialog")
        dialog.child(name="Show values").click()
        time.sleep(0.2)  # Avoid 'GTK_IS_RANGE (range)' failed
        spinbtns = self._find_by_role("spin button", dialog)
        spinbtns[0].click()
        spinbtns[0].text = "10"
        spinbtns[1].click()
        spinbtns[1].text = "15"
        dialog.child(name="OK").click()
        self._wait_cond(lambda: dialog.dead)

    def test_05_past_underlay(self):
        """Past a page with overlay under an other page"""
        app = self._app()
        app.keyCombo("<ctrl>c")
        app.keyCombo("Left")
        app.keyCombo("<shift><ctrl>u")
        dialog = self._app().child(roleName="dialog")
        dialog.child(name="OK").click()
        self._wait_cond(lambda: dialog.dead)

    def test_06_export(self):
        self._mainmenu(["Export", "Export All Pages to Individual Files…"])
        self._save_as_chooser(
            "alltosingle.pdf", ["alltosingle.pdf", "alltosingle-002.pdf"]
        )
        self._assert_file_size("alltosingle.pdf", 1800)
        self._assert_file_size("alltosingle-002.pdf", 1544)
        self._assert_page_content("alltosingle.pdf", (
            b'1 0 0 rg 530 180 m 70 180 l 300 580 l h 530 180 m B',
            b'  /BBox [', b'    68\n', b'    179\n', b'    532\n', b'    582\n',
            b'1 0 0 rg 530 180 m 70 180 l 300 580 l h 530 180 m B'))
        self._assert_page_content("alltosingle.pdf", (
            b'  /BBox [', b'    0\n', b"    0\n", b'    612\n', b'    792\n',
            b'0 1 0 rg 530 180 m 70 180 l 300 580 l h 530 180 m B'))

    def test_07_clear(self):
        self._popupmenu(1, "Delete")
        self.assertEqual(len(self._icons()), 1)

    def test_08_about(self):
        self._mainmenu("About")
        dialog = self._app().child(roleName="dialog")
        dialog.child(name="Close").click()
        self._wait_cond(lambda: dialog.dead)

    def test_09_quit(self):
        self._quit_without_saving()


class TestBatch3(PdfArrangerTest):
    """Test encryption"""
    def test_01_open_encrypted(self):
        filename = os.path.join(self.__class__.tmp, "other_encrypted.pdf")
        shutil.copyfile("tests/test_encrypted.pdf", filename)
        self._start([filename])
        dialog = self._app().child(roleName="dialog")
        passfield = dialog.child(roleName="password text")
        passfield.text = "foobar"
        dialog.child(name="OK").click()
        self._wait_cond(lambda: dialog.dead)

    def test_02_import_wrong_pass(self):
        filechooser = self._import_file("tests/test_encrypted.pdf")
        dialog = self._app().child(roleName="dialog")
        passfield = dialog.child(roleName="password text")
        passfield.text = "wrong"
        dialog.child(name="OK").click()
        dialog = self._app().child(roleName="dialog")
        dialog.child(name="Cancel").click()
        self._wait_cond(lambda: dialog.dead)
        self._wait_cond(lambda: filechooser.dead)
        self.assertEqual(len(self._icons()), 2)

    def test_03_quit(self):
        app = self._app()
        app.keyCombo("<ctrl>z")  # undo
        app.keyCombo("<ctrl>y")  # redo
        self._quit()
        dialog = self._app().child(roleName="alert")
        dialog.child(name="Save").click()
        filechooser = self._app().child(roleName="file chooser")
        filechooser.button("Save").click()
        dialog = self._app().child(roleName="alert")
        dialog.child(name="Replace").click()
        # check that process actually exit
        self._process().wait(timeout=22)


class TestBatch4(PdfArrangerTest):
    """Check the size of duplicated and scaled pages"""
    def test_01_import_pdf(self):
        self._start(["tests/test.pdf"])

    def test_02_duplicate(self):
        app = self._app()
        app.keyCombo("Down")
        self._popupmenu(0, ["Duplicate"])
        app.keyCombo("Right")

    def test_03_scale(self):
        self._scale_selected(200)
        self._app().keyCombo("<ctrl>Left")  # rotate left
        self._assert_selected("2")
        self._assert_page_size(558.8, 431.8)

    def test_04_export(self):
        app = self._app()
        app.keyCombo("<ctrl>a")  # select all
        self._mainmenu(["Export", "Export Selection to a Single File…"])
        self._save_as_chooser("scaled.pdf")
        self._popupmenu(0, "Delete")

    def test_05_import(self):
        filename = os.path.join(self.__class__.tmp, "scaled.pdf")
        filechooser = self._import_file(filename)
        self._wait_cond(lambda: filechooser.dead)
        self.assertEqual(len(self._icons()), 3)
        app = self._app()
        self._app().child(roleName="layered pane").grabFocus()
        app.keyCombo("Right")
        app.keyCombo("Right")
        self._assert_selected("2")
        self._assert_page_size(558.8, 431.8)
        self._quit_without_saving()


class TestBatch5(PdfArrangerTest):
    """Test booklet and blank pages"""
    def test_01_import_pdf(self):
        self._start(["tests/test.pdf"])

    def test_02_blank_page(self):
        self._popupmenu(None, ["Select", "Select All"])
        self._popupmenu(0, ["Crop White Borders"])
        self._scale_selected(150)
        self._popupmenu(0, ["Insert Blank Page…"])
        dialog = self._app().child(roleName="dialog")
        dialog.child(name="OK").click()
        self._wait_cond(lambda: len(self._icons()) == 3)

    def test_03_booklet(self):
        self._popupmenu(0, ["Select", "Select All"])
        self._popupmenu(0, ["Booklet", "Generate (imposition)"])
        self._wait_cond(lambda: len(self._icons()) == 2)
        self._app().child(roleName="layered pane").keyCombo("Home")
        self._assert_page_size(491.1, 213.3)
        self._app().child(roleName="layered pane").keyCombo("End")
        self._assert_page_size(491.1, 213.3)

    def test_04_crop_white_border(self):
        # Test selection with shift+arrow
        self._app().child(roleName="layered pane").grabFocus()
        self._app().child(roleName="layered pane").keyCombo("<shift>Left")
        self._assert_selected("1-2")
        self._popupmenu(0, ["Crop White Borders"])
        self._assert_page_size(245.2, 213.3, 0)
        self._assert_page_size(245.2, 213.3, 1)

    def test_05_split_booklet(self):
        self._popupmenu(0, ["Select", "Select All"])
        self._popupmenu(0, ["Booklet", "Generate (imposition)"])
        self._popupmenu(0, ["Select", "Select All"])
        self._popupmenu(0, ["Booklet", "Split (unimposition)"])
        # OK OK hear me out generating the booklet took 2 pages and made it 2 after the process
        # by adding two blank pages at the end. So we should have 4 now.
        # They should have 245.2x213.3mm size like in test_04 because
        # test.pdf is only loaded once for all operations.
        self._wait_cond(lambda: len(self._icons()) == 4)
        self._app().child(roleName="layered pane").keyCombo("Home")
        self._assert_page_size(245.2, 213.3)
        self._app().child(roleName="layered pane").keyCombo("End")
        self._assert_page_size(245.2, 213.3)

    def test_06_buggy_exif(self):
        """Test img2pdf import with buggy EXIF rotation"""
        if not check_img2pdf([0,4,2]):
            print("Ignoring test_05_buggy_exif, img2pdf too old")
            return
        filechooser = self._import_file("tests/1x1.jpg")
        self._wait_cond(lambda: filechooser.dead)
        self.assertEqual(len(self._icons()), 5)

    def test_07_quit(self):
        self._quit_without_saving()


class TestBatch6(PdfArrangerTest):
    """Test hide margins and merge pages"""
    def test_01_import_pdf(self):
        self._start(["tests/test.pdf"])

    def test_02_merge_pages(self):
        self._app().keyCombo("<ctrl>a")
        self._popupmenu(0, "Merge Pages…")
        dialog = self._app().child(roleName="dialog")
        dialog.child(name="OK").click()
        self._wait_cond(lambda: dialog.dead)

    def test_03_crop_margins(self):
        self._app().keyCombo("<ctrl>a")
        self._popupmenu(0, "Crop Margins…")
        dialog = self._app().child(roleName="dialog")
        croppanel = dialog.child(name="Crop Margins")
        cropbuttons = self._find_by_role("spin button", croppanel)
        dialog.child(name="Show values").click()
        for i in range(4):
            cropbuttons[i].typeText("2")
        dialog.child(name="OK").click()
        self._wait_cond(lambda: dialog.dead)
        self._assert_page_size(414.5, 268.2)

    def test_04_hide_margins(self):
        self._app().keyCombo("<ctrl>a")
        self._assert_selected("1")
        self._app().keyCombo("H")
        dialog = self._app().child(roleName="dialog")
        da = dialog.child(roleName="drawing area")
        page_x = da.position[0] + 25  # 25 = padding in DrawingAreaWidget
        page_width = da.size[0] - 50
        x_center = da.position[0] + da.size[0] / 2
        y_center = da.position[1] + da.size[1] / 2
        from dogtail import rawinput
        for button in ["Apply", "Revert", "Apply"]:
            rawinput.drag((page_x, y_center), (page_x + page_width * 0.8, y_center))
            rawinput.drag((page_x + page_width * 0.9, y_center), (page_x + page_width * 0.3, y_center))
            dialog.child(name=button).click()
        hidepanel = dialog.child(name="Hide Margins")
        hidebuttons = self._find_by_role("spin button", hidepanel)
        hidebuttons[2].text = str(round(float(hidebuttons[2].text) / 10) * 10)
        hidebuttons[3].text = str(round(float(hidebuttons[3].text) / 10) * 10)
        self.assertEqual(hidebuttons[2].text, "60")
        self.assertEqual(hidebuttons[3].text, "20")
        self._zoom(da, 15, zoom_in=True)
        rawinput.drag((x_center, y_center), (x_center + 10, y_center + 20),  button=2)  # pan view
        self._zoom(da, 15, zoom_in=False)
        dialog.child(name="OK").click()
        self._wait_cond(lambda: dialog.dead)

    def test_05_export(self):
        self._popupmenu(0, ["Select", "Select All"])
        self._mainmenu(["Export", "Export Selection to a Single File…"])
        self._save_as_chooser("hide.pdf")
        self._assert_file_size("hide.pdf", 1726)

    def test_06_merge_pages(self):
        self._popupmenu(0, ["Select", "Select All"])
        self._popupmenu(0, "Merge Pages…")
        dialog = self._app().child(roleName="dialog")
        orderpanel = dialog.child(name="Page Order")
        radiobuttons = self._find_by_role("radio button", orderpanel)
        radiobuttons[0].click()
        radiobuttons[2].click()
        radiobuttons[4].click()
        dialog.child(name="OK").click()
        self._wait_cond(lambda: dialog.dead)
        self._assert_page_size(829.1, 268.2)

    def test_07_quit(self):
        self._quit_without_saving()


class TestBatch7(PdfArrangerTest):
    """Test extracting of images and text"""
    def test_01_import_pdf(self):
        self._start(["tests/test_raster_image_text.pdf"])

    def test_02_explode_images(self):
        self._popupmenu(1, ["Select", "Select All"])
        all_selected = all([icon.selected for icon in self._icons()])
        self._wait_cond(lambda: all_selected)
        self._popupmenu(1, ["Extract", "Explode into Images"])
        self._wait_cond(lambda: len(self._icons()) == 5)

    def test_03_copy_and_paste_image(self):
        """Copy image from page which has one image"""
        self._icons()[0].click(button=1)
        self._popupmenu(0, ["Extract", "Copy Image"])
        self._app().keyCombo("<ctrl>v")
        self._wait_cond(lambda: len(self._icons()) == 6)

    def test_04_copy_and_paste_image(self):
        """Try to copy image from page which has two images"""
        self._popupmenu(2, ["Extract", "Copy Image"])
        filler = self._find_by_role("filler")[-1]
        filler.child(name="OK").click()
        self._wait_cond(lambda: filler.dead)
        self._popupmenu(1, ["Paste After"])
        self._wait_cond(lambda: len(self._icons()) == 7)

    def test_05_copy_text(self):
        """Copy text "tests/test_raster_image_text.pdf" from pdf"""
        self._popupmenu(0, ["Extract", "Copy Text"])
        self._app().keyCombo("<ctrl>v")
        self._wait_cond(lambda: len(self._icons()) == 9)

    def test_06_quit(self):
        self._quit_without_saving()


class TestBatch8(PdfArrangerTest):
    """Test Open action"""

    def test_01_open_empty(self):
        self._start()

    def test_02_open(self):
        filechooser = self._import_file("tests/test.pdf", open_action=True)
        self._wait_cond(lambda: filechooser.dead)

    def test_03_open_again(self):
        """Create a new pdfarranger instance"""
        self.assertEqual(len(self._apps()), 1)
        filechooser = self._import_file("tests/test.pdf", open_action=True)
        self._wait_cond(lambda: filechooser.dead)
        self._wait_cond(lambda: len(self._apps()) == 2)

    def test_04_quit(self):
        """Quit the second instance"""
        self._quit()
        self._wait_cond(lambda: len(self._apps()) == 1)

    def test_05_quit(self):
        """Quit the first instance"""
        self._quit()
        self._process().wait(timeout=22)

class TestBatch9(PdfArrangerTest):
    """test batch for cli flags """

    def test_01_cli_version(self):
        """test version flag"""
        self._start(["--version"], gtk_check=False)
        time.sleep(0.5)
        stdout = self._get_stdout()
        self.assertIn("pikepdf", stdout)
        self.assertIn("libqpdf", stdout)
        self.assertIn("OS-", stdout)
        self.assertIn("OS_Version-", stdout)


class TestBatch10(PdfArrangerTest):
    """Test start-with-empty config setting"""

    def test_01_default(self):
        """Test default setting"""
        self._start(["tests/test_encrypted.pdf"])
        dialog = self._app().child(roleName="dialog")
        passfield = dialog.child(roleName="password text")
        passfield.text = "foobar"
        dialog.child(name="OK").click()
        self._wait_cond(lambda: dialog.dead)

        self._mainmenu("Preferences")
        dialog = self._app().child(roleName="dialog")
        savepanel_name = "Saving/exporting to single file"
        if have_pikepdf8():
            savepanel = dialog.child(name=savepanel_name)
            checkboxes = self._find_by_role("check box", savepanel)
            self.assertTrue(checkboxes[0].checked)
        else:
            panels = self._find_by_role("panel", dialog)
            self.assertNotIn(savepanel_name, [p.name for p in panels])
        dialog.child(name="OK").click()
        self._wait_cond(lambda: dialog.dead)

        self._mainmenu("Save As…")
        self._save_as_chooser("encrypted.pdf", ["encrypted.pdf"] )
        if have_pikepdf8():
            with warnings.catch_warnings(record=True) as w:
                with pikepdf.open(os.path.join(self.__class__.tmp, "encrypted.pdf"), password="foobar"):
                    pass
            # there should be no warning that no password was needed to open this PDF
            self.assertEqual(len(w), 0)
        else:
            with pikepdf.open(os.path.join(self.__class__.tmp, "encrypted.pdf")):
                # there should be no pikepdf._core.PasswordError for pikepdf < 8
                pass

    def test_02_legacy(self):
        """Test legacy setting"""
        if have_pikepdf8():
            self._mainmenu("Preferences")
            dialog = self._app().child(roleName="dialog")
            savepanel = dialog.child(name="Saving/exporting to single file")
            checkboxes = self._find_by_role("check box", savepanel)
            checkboxes[0].click()
            dialog.child(name="OK").click()
            self.assertFalse(checkboxes[0].checked)
            self._wait_cond(lambda: dialog.dead)

        self._mainmenu("Save As…")
        self._save_as_chooser("unencrypted.pdf", ["unencrypted.pdf"])
        with pikepdf.open(os.path.join(self.__class__.tmp, "unencrypted.pdf")):
            # there should be no pikepdf._core.PasswordError
            pass

    def test_03_quit(self):
        self._quit()  # save config
        self._process().wait(timeout=22)
        if have_pikepdf8():
            self.assertEqual(self._config()["preferences"]["start-with-empty"], "True")


class TestBatch11(PdfArrangerTest):
    """Test show-save-warnings config setting and metadata"""

    def test_01_save_and_disable_warning(self):
        """Show warning discarded metadata (default)"""
        self._start(["tests/test_metadata1.pdf"])
        self._mainmenu("Save")
        self._save_as_chooser("output1.pdf")
        alert = self._find_by_role("alert")[0]
        from dogtail import predicate
        alert.findChild(predicate.IsNamed("Saving produced some warnings"), requireResult=True)
        check_box = self._find_by_role("check box", alert)[0]
        check_box.click()  # Don't show warnings when saving again
        alert.button("OK").click()
        self._wait_cond(lambda: alert.dead)

    def test_02_save_without_warning(self):
        """Don't show warning discarded metadata"""
        self._mainmenu("Save")
        self._wait_saving()

    def test_03_retain_metadata(self):
        """Merge metadata into first file and don't overwrite from info dict"""
        filechooser = self._import_file("tests/test_metadata2.pdf")
        self._wait_cond(lambda: filechooser.dead)
        self._mainmenu("Save As…")
        self._save_as_chooser("output2.pdf", ["output2.pdf"])
        with pikepdf.Pdf.open(os.path.join(self.__class__.tmp, "output2.pdf")) as pdf:
            del pdf.docinfo["/Producer"]  # pikepdf version will vary
            expected = {
                "/Title": "Title1",
                "/Author": "First; Second",
                "/Subject": "Subject2",
                "/Custom": "info dict entry",
            }
            if not have_pikepdf8():
                del expected["/Custom"]
            self.assertDictEqual(dict(pdf.docinfo), expected)
            with pdf.open_metadata(False, False) as meta:
                del meta["pdf:Producer"]      # pikepdf version will vary
                del meta["xmp:MetadataDate"]  # will vary
                exptected = {
                    # xmp metadata with info dict equivalents
                    "{http://purl.org/dc/elements/1.1/}title": "Title1",
                    "{http://purl.org/dc/elements/1.1/}creator": ["First", "Second"],
                    "{http://purl.org/dc/elements/1.1/}description": "Subject2",
                    # xmp metadata without info dict equivalents
                    "{http://purl.org/dc/elements/1.1/}identifier": "ID1",
                    "{http://purl.org/dc/elements/1.1/}source": "Source2",
                    "{http://ns.adobe.com/xap/1.0/}label": "Label1",
                }
                self.assertDictEqual(dict(meta), exptected)

    def test_04_quit(self):
        """Test config settings persistence"""
        self._quit()
        self._process().wait(timeout=22)
        self.assertEqual(self._config()["preferences"]["show-save-warnings"], "False")


class TestBatch12(PdfArrangerTest):
    """Test printing"""

    def test_01_open_documents(self):
        self._start(["data/screenshot.png", "tests/test.pdf", "tests/test_metadata1.pdf"])
        # Remove last page
        self._popupmenu(3, ["Cut"])

    def test_02_print_all_to_pdf(self):
        """Print with Gtk.PrintUnixDialog"""
        self._mainmenu(["Print…"])
        print_dialog = self._app().child(roleName="dialog")
        print_dialog.child(name="Print to File").click()
        from dogtail import rawinput
        rawinput.keyCombo("Tab")
        # Open "file chooser" to set the output name
        rawinput.keyCombo("enter")
        filechooser = self._app().child(roleName="file chooser")
        output = os.path.join(self.__class__.tmp, 'output.pdf')
        filechooser.child(roleName="text").text = output
        filechooser.button("Select").click()
        self._wait_cond(lambda: filechooser.dead)
        print_dialog.button("Print").click()
        self._wait_cond(lambda: print_dialog.dead)
        # Import the printed pdf
        filechooser = self._import_file(output)
        self._wait_cond(lambda: filechooser.dead)
        self.assertEqual(len(self._icons()), 6)

    def test_03_quit(self):
        self._quit_without_saving()


class TestBatch13(PdfArrangerTest):
    """Test export as jpg images"""

    def test_01_import_pdf(self):
        self._start(["tests/test.pdf"])

    def test_02_export_as_png(self):
        """Export image at default 150 pixels/inch"""
        self._popupmenu(None, ["Select", "Select All"])
        self._mainmenu(["Export", "Export Selection to JPG Images…"])
        self._save_as_chooser("image-001.jpg")

    def test_03_import_png(self):
        """Import the two images that were created"""
        tmp = self.__class__.tmp
        filename = os.path.join(tmp, "image-001.jpg")
        filechooser = self._import_file(filename)
        self._wait_cond(lambda: filechooser.dead)
        filename = os.path.join(tmp, "image-002.jpg")
        filechooser = self._import_file(filename)
        self._wait_cond(lambda: filechooser.dead)
        self.assertEqual(len(self._icons()), 4)
        self._app().child(roleName="layered pane").grabFocus()
        self._app().keyCombo("End")
        self._assert_page_size(215.9, 279.4)

    def test_04_quit(self):
        self._quit_without_saving()


class TestBatch14(PdfArrangerTest):
    """Test export as png images"""

    def test_01_import_pdf(self):
        self._start(["tests/test.pdf"])

    def test_02_export_as_png(self):
        """Export image at default 150 pixels/inch"""
        self._popupmenu(None, ["Select", "Select All"])
        self._mainmenu(["Export", "Export Selection to PNG Images…"])
        self._save_as_chooser("image-001.png")

    def test_03_close(self):
        """Test close application"""
        self._app().child(roleName="layered pane").grabFocus()
        self._app().keyCombo("<ctrl>W")
        self.assertEqual(len(self._icons()), 0)

    def test_04_import_png(self):
        """Import the two images that were created"""
        tmp = self.__class__.tmp
        filename1 = os.path.join(tmp, "image-001.png")
        filename2 = os.path.join(tmp, "image-002.png")
        filechooser = self._import_file(filename1)
        self._wait_cond(lambda: filechooser.dead)
        filechooser = self._import_file(filename2)
        self._wait_cond(lambda: filechooser.dead)
        self.assertEqual(len(self._icons()), 2)
        self._app().child(roleName="layered pane").grabFocus()
        self._app().keyCombo("End")
        self._assert_page_size(215.9, 279.4)

    def test_05_quit(self):
        self._quit_without_saving()


class TestBatch15(PdfArrangerTest):
    """Test export as Rasterized PDF"""

    def test_01_import_pdf(self):
        self._start(["tests/test.pdf"])

    def test_02_export_as_pdf(self):
        """Export image at 150 pixels/inch as optimized greyscale"""
        self._mainmenu("Preferences")
        dialog = self._app().child(roleName="dialog")
        imagepanel = dialog.child(name="Image Export")
        checkboxes = self._find_by_role("check box", imagepanel)
        checkboxes[0].click()  # Greyscale
        checkboxes[1].click()  # Optimize
        dialog.child(name="OK").click()
        self._wait_cond(lambda: dialog.dead)
        self._popupmenu(1, ["Rotate Left"])
        self._app().child(roleName="layered pane").grabFocus()
        self._popupmenu(None, ["Select", "Select All"])
        self._mainmenu(["Export", "Export Selection to Rasterized PDF (png)…"])
        self._save_as_chooser("pdf-image-001.pdf")

    def test_03_import_png(self):
        """Import the pdf that was created"""
        tmp = self.__class__.tmp
        filename = os.path.join(tmp, "pdf-image-001.pdf")
        filechooser = self._import_file(filename)
        self._wait_cond(lambda: filechooser.dead)
        self.assertEqual(len(self._icons()), 4)
        self._app().child(roleName="layered pane").grabFocus()
        self._app().keyCombo("End")
        self._assert_page_size(279.4, 215.9)

    def test_04_quit(self):
        self._quit_without_saving()
