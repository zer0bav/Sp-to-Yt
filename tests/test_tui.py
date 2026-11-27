from textual.widgets import DataTable, OptionList, SelectionList

from tunebridge.tui.app import PlaylistsScreen, RunScreen, TuneBridgeApp


async def test_tui_end_to_end():
    app = TuneBridgeApp()
    async with app.run_test(size=(150, 42)) as pilot:
        await pilot.pause()
        scr = app.screen
        src, dst = scr.query_one("#source", OptionList), scr.query_one("#target", OptionList)
        ids = [src.get_option_at_index(i).id for i in range(src.option_count)]
        src.highlighted, dst.highlighted = ids.index("demo"), ids.index("demo2")
        await pilot.pause()
        scr.action_next()
        for _ in range(50):
            await pilot.pause(0.05)
            if isinstance(app.screen, PlaylistsScreen) and app.screen.all:
                break
        lst = app.screen.query_one("#pl-list", SelectionList)
        lst.select(lst.get_option_at_index(1).value)  # Focus
        await pilot.pause()
        app.screen.go()
        await pilot.pause()
        assert isinstance(app.screen, RunScreen)
        for _ in range(100):
            await pilot.pause(0.05)
            if app.screen.phase == "review":
                break
        assert app.screen.query_one("#results", DataTable).row_count == 5
        await pilot.press("t")
        for _ in range(100):
            await pilot.pause(0.05)
            if app.screen.phase == "done":
                break
        assert app.screen.phase == "done"
        assert TuneBridgeApp  # app still alive
        await pilot.press("ctrl+t")  # theme switch must not crash
        await pilot.pause()
