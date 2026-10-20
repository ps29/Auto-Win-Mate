Add-Type -AssemblyName System.Windows.Forms
$state = Join-Path $PSScriptRoot "teststate.txt"
$f = New-Object Windows.Forms.Form -Property @{Text="WinControlTest"; Width=420; Height=240; StartPosition="Manual"; Left=300; Top=300}
$filled = New-Object Windows.Forms.TextBox -Property @{Name="filledBox"; Text="existing user text"; Left=20; Top=20; Width=300}
$empty = New-Object Windows.Forms.TextBox -Property @{Name="emptyBox"; Left=20; Top=55; Width=300}
$lbl = New-Object Windows.Forms.Label -Property @{Text="clicks: 0"; Left=20; Top=90; Width=200}
$btn = New-Object Windows.Forms.Button -Property @{Text="Press me"; Left=20; Top=120; Width=100}
$script:n = 0
$btn.Add_Click({ $script:n++; $lbl.Text = "clicks: $script:n" })
$t = New-Object Windows.Forms.Timer -Property @{Interval=300}
$t.Add_Tick({ Set-Content $state "filled=$($filled.Text)|empty=$($empty.Text)|clicks=$script:n" })
$t.Start()
$k = New-Object Windows.Forms.Timer -Property @{Interval=900000}; $k.Add_Tick({ $f.Close() }); $k.Start()
$f.Controls.AddRange(@($filled, $empty, $lbl, $btn))
[void]$f.ShowDialog()
