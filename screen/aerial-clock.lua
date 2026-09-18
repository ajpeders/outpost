-- Live ticking seconds for the aerial screensaver clock.
--
-- The dashboard overlay is a PNG re-rendered only once a minute (a full 4K
-- headless-chromium render takes ~4s), so its clock can't show live seconds.
-- Instead mpv draws just the seconds here as an ASS OSD, updated every second —
-- essentially free. Positioned in the dashboard clock's empty #sec slot, to the
-- left of the AM/PM, matching the page's white DejaVu Sans bold. Tune
-- position/size via the AERIAL_SEC_* env vars without editing this file.

local X  = tonumber(os.getenv("AERIAL_SEC_X")  or "1133")   -- 3840x2160 space; #sec slot center-x
local Y  = tonumber(os.getenv("AERIAL_SEC_Y")  or "351")    -- AM/PM line vertical center
local FS = tonumber(os.getenv("AERIAL_SEC_FS") or "115")    -- #sec font size

local ov = mp.create_osd_overlay("ass-events")
ov.res_x = 3840
ov.res_y = 2160

local function update()
    local s = os.date("*t").sec
    -- \an5 = middle-centre: the empty #sec slot has zero height, so anchoring
    -- its top would drop the seconds into the date below. Centre on the AM/PM.
    ov.data = string.format(
        "{\\an5\\pos(%d,%d)\\fnDejaVu Sans\\b1\\fs%d\\c&HFFFFFF&"
        .. "\\bord0\\shad5\\4c&H000000&\\4a&H40&}:%02d",
        X, Y, FS, s)
    ov:update()
end

mp.add_periodic_timer(1, update)
update()
