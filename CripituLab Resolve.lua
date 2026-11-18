-- The installer prepends the local paths used by this menu entry.
local app = (bmd and bmd.scriptapp("Resolve")) or resolve
local manager = app and app:GetProjectManager()
local project = manager and manager:GetCurrentProject()
local timeline = project and project:GetCurrentTimeline()
if not timeline then
    error("CripituLab: abra a timeline que contém os dois brutos.")
end

local snapshot
for i = 1, 1000 do
    local path = snapshot_folder .. "/timeline-" .. os.time() .. "-" .. i .. ".xml"
    if not bmd.fileexists(path) then
        snapshot = path
        break
    end
end
if not snapshot or not timeline:Export(snapshot, app.EXPORT_FCP_7_XML, app.EXPORT_NONE) then
    error("CripituLab: o Resolve não conseguiu exportar a lista de clipes da timeline.")
end
print("CripituLab: clipes da timeline " .. timeline:GetName() .. " enviados.")
local mailbox = snapshot .. ".bridge"
local function write(path, value)
    local file = io.open(path, "wb")
    if not file then return false end
    file:write(value)
    file:close()
    return true
end
local connected = io and io.open and write(mailbox .. ".ready", "ready")
if os.execute then
    local command = 'start "" "' .. python_gui .. '" "' .. editor_script
        .. '" --resolve-timeline "' .. snapshot .. '"'
    local ok, result = pcall(os.execute, command)
    if not ok or (result ~= true and result ~= 0) then
        print("CripituLab: abra a janela pelo atalho do Windows e clique em Ler timeline do Resolve.")
    end
end
if not connected then return end

local project_id = project:GetUniqueId()
local function respond(action, path, ticket)
    local current = manager:GetCurrentProject()
    if not current or current:GetUniqueId() ~= project_id then
        return "error\nO projeto aberto mudou. Abra o CripituLab pelo menu do projeto de destino."
    end
    if action == "read" then
        local active = current:GetCurrentTimeline()
        local fresh = snapshot .. "." .. ticket .. ".xml"
        if active and active:Export(fresh, app.EXPORT_FCP_7_XML, app.EXPORT_NONE) then
            return "ok\n" .. fresh
        end
        return "error\nAbra uma timeline com os clipes de origem."
    end
    if action ~= "send" or not path:lower():match("%.fcpxml$") or not bmd.fileexists(path) then
        return "error\nArquivo de montagem indisponível."
    end
    local result = current:GetMediaPool():ImportTimelineFromFile(path, { importSourceClips = true })
    if not result then
        return "error\nO Resolve não conseguiu criar a timeline."
    end
    if not current:SetCurrentTimeline(result) then
        return "ok\nCriada no Media Pool: " .. result:GetName()
    end
    return "ok\n" .. result:GetName()
end

local expires = os.time() + 900
local last_ticket = ""
while os.time() < expires and not bmd.fileexists(mailbox .. ".stop") do
    local file = io.open(mailbox .. ".request", "rb")
    if file then
        local ticket = file:read("*l")
        local action = file:read("*l")
        local path = file:read("*l") or ""
        file:close()
        if ticket and ticket:match("^%x+$") and ticket ~= last_ticket then
            last_ticket = ticket
            expires = os.time() + 900
            os.remove(mailbox .. ".request")
            local ok, answer = pcall(respond, action, path, ticket)
            write(mailbox .. "." .. ticket, ok and answer or ("error\n" .. tostring(answer)))
            if ok and action == "send" and answer:sub(1, 3) == "ok\n" then break end
        end
    end
    bmd.wait(0.50)
end
write(mailbox .. ".closed", "closed")
