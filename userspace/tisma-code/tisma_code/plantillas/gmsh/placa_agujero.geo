// Plantilla TISMA: placa rectangular con agujero central (2D, mm), malla refinada junto al agujero.
// Útil como dominio para elementos finitos (concentración de tensiones: Kt ≈ 3 en placa ancha).
// Malla 2D: ingenieria accion=gmsh opciones={"dimension": 2}
SetFactory("OpenCASCADE");
L = 100; H = 50; r = 8;
h_lejos = 5; h_cerca = 0.8;
Rectangle(1) = {-L/2, -H/2, 0, L, H};
Disk(2) = {0, 0, 0, r};
BooleanDifference(3) = { Surface{1}; Delete; }{ Surface{2}; Delete; };
MeshSize{ PointsOf{ Surface{3}; } } = h_lejos;
hueco() = Curve In BoundingBox{-r-0.1, -r-0.1, -1, r+0.1, r+0.1, 1};
Field[1] = Distance; Field[1].CurvesList = {hueco()};
Field[2] = Threshold; Field[2].InField = 1;
Field[2].SizeMin = h_cerca; Field[2].SizeMax = h_lejos; Field[2].DistMin = 1; Field[2].DistMax = 20;
Background Field = 2;
Physical Surface("placa") = {3};
